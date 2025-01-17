# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2011 OpenStack Foundation.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""
Utility methods for working with WSGI servers
"""
from __future__ import print_function

import errno
import os
import socket
import ssl
import sys
import time
from xml.etree import ElementTree as etree
from xml.parsers import expat

import eventlet.wsgi
eventlet.patcher.monkey_patch(all=False, socket=True)
from oslo.config import cfg
import routes.middleware
import webob.dec
import webob.exc

from neutron.common import constants
from neutron.common import exceptions as exception
from neutron import context
from neutron.db import api
from neutron.openstack.common import excutils
from neutron.openstack.common import gettextutils
from neutron.openstack.common import jsonutils
from neutron.openstack.common import log as logging
from neutron.openstack.common import service as common_service

socket_opts = [
    cfg.IntOpt('backlog',
               default=4096,
               help=_("Number of backlog requests to configure "
                      "the socket with")),
    cfg.IntOpt('tcp_keepidle',
               default=600,
               help=_("Sets the value of TCP_KEEPIDLE in seconds for each "
                      "server socket. Not supported on OS X.")),
    cfg.IntOpt('retry_until_window',
               default=30,
               help=_("Number of seconds to keep retrying to listen")),
    cfg.IntOpt('max_header_line',
               default=16384,
               help=_("Max header line to accommodate large tokens")),
    cfg.BoolOpt('use_ssl',
                default=False,
                help=_('Enable SSL on the API server')),
    cfg.StrOpt('ssl_ca_file',
               help=_("CA certificate file to use to verify "
                      "connecting clients")),
    cfg.StrOpt('ssl_cert_file',
               help=_("Certificate file to use when starting "
                      "the server securely")),
    cfg.StrOpt('ssl_key_file',
               help=_("Private key file to use when starting "
                      "the server securely")),
]

CONF = cfg.CONF
CONF.register_opts(socket_opts)

LOG = logging.getLogger(__name__)


class WorkerService(object):
    """Wraps a worker to be handled by ProcessLauncher"""
    def __init__(self, service, application):
        self._service = service
        self._application = application
        self._server = None

    def start(self):
        # We may have just forked from parent process.  A quick disposal of the
        # existing sql connections avoids producting 500 errors later when they
        # are discovered to be broken.
        api.get_engine().pool.dispose()
        self._server = self._service.pool.spawn(self._service._run,
                                                self._application,
                                                self._service._socket)

    def wait(self):
        self._service.pool.waitall()

    def stop(self):
        if isinstance(self._server, eventlet.greenthread.GreenThread):
            self._server.kill()
            self._server = None


class Server(object):
    """Server class to manage multiple WSGI sockets and applications."""

    def __init__(self, name, threads=1000):
        # Raise the default from 8192 to accommodate large tokens
        eventlet.wsgi.MAX_HEADER_LINE = CONF.max_header_line
        self.pool = eventlet.GreenPool(threads)
        self.name = name
        self._launcher = None
        self._server = None

    def _get_socket(self, host, port, backlog):
        bind_addr = (host, port)
        # TODO(dims): eventlet's green dns/socket module does not actually
        # support IPv6 in getaddrinfo(). We need to get around this in the
        # future or monitor upstream for a fix
        try:
            info = socket.getaddrinfo(bind_addr[0],
                                      bind_addr[1],
                                      socket.AF_UNSPEC,
                                      socket.SOCK_STREAM)[0]
            family = info[0]
            bind_addr = info[-1]
        except Exception:
            LOG.exception(_("Unable to listen on %(host)s:%(port)s"),
                          {'host': host, 'port': port})
            sys.exit(1)

        if CONF.use_ssl:
            if not os.path.exists(CONF.ssl_cert_file):
                raise RuntimeError(_("Unable to find ssl_cert_file "
                                     ": %s") % CONF.ssl_cert_file)

            # ssl_key_file is optional because the key may be embedded in the
            # certificate file
            if CONF.ssl_key_file and not os.path.exists(CONF.ssl_key_file):
                raise RuntimeError(_("Unable to find "
                                     "ssl_key_file : %s") % CONF.ssl_key_file)

            # ssl_ca_file is optional
            if CONF.ssl_ca_file and not os.path.exists(CONF.ssl_ca_file):
                raise RuntimeError(_("Unable to find ssl_ca_file "
                                     ": %s") % CONF.ssl_ca_file)

        def wrap_ssl(sock):
            ssl_kwargs = {
                'server_side': True,
                'certfile': CONF.ssl_cert_file,
                'keyfile': CONF.ssl_key_file,
                'cert_reqs': ssl.CERT_NONE,
            }

            if CONF.ssl_ca_file:
                ssl_kwargs['ca_certs'] = CONF.ssl_ca_file
                ssl_kwargs['cert_reqs'] = ssl.CERT_REQUIRED

            return ssl.wrap_socket(sock, **ssl_kwargs)

        sock = None
        retry_until = time.time() + CONF.retry_until_window
        while not sock and time.time() < retry_until:
            try:
                sock = eventlet.listen(bind_addr,
                                       backlog=backlog,
                                       family=family)
                if CONF.use_ssl:
                    sock = wrap_ssl(sock)

            except socket.error as err:
                with excutils.save_and_reraise_exception() as ctxt:
                    if err.errno == errno.EADDRINUSE:
                        ctxt.reraise = False
                        eventlet.sleep(0.1)
        if not sock:
            raise RuntimeError(_("Could not bind to %(host)s:%(port)s "
                               "after trying for %(time)d seconds") %
                               {'host': host,
                                'port': port,
                                'time': CONF.retry_until_window})
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # sockets can hang around forever without keepalive
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)

        # This option isn't available in the OS X version of eventlet
        if hasattr(socket, 'TCP_KEEPIDLE'):
            sock.setsockopt(socket.IPPROTO_TCP,
                            socket.TCP_KEEPIDLE,
                            CONF.tcp_keepidle)

        return sock

    def start(self, application, port, host='0.0.0.0', workers=0):
        """Run a WSGI server with the given application."""
        self._host = host
        self._port = port
        backlog = CONF.backlog

        self._socket = self._get_socket(self._host,
                                        self._port,
                                        backlog=backlog)
        if workers < 1:
            # For the case where only one process is required.
            self._server = self.pool.spawn(self._run, application,
                                           self._socket)
        else:
            # Minimize the cost of checking for child exit by extending the
            # wait interval past the default of 0.01s.
            self._launcher = common_service.ProcessLauncher(wait_interval=1.0)
            self._server = WorkerService(self, application)
            self._launcher.launch_service(self._server, workers=workers)

    @property
    def host(self):
        return self._socket.getsockname()[0] if self._socket else self._host

    @property
    def port(self):
        return self._socket.getsockname()[1] if self._socket else self._port

    def stop(self):
        if self._launcher:
            # The process launcher does not support stop or kill.
            self._launcher.running = False
        else:
            self._server.kill()

    def wait(self):
        """Wait until all servers have completed running."""
        try:
            if self._launcher:
                self._launcher.wait()
            else:
                self.pool.waitall()
        except KeyboardInterrupt:
            pass

    def _run(self, application, socket):
        """Start a WSGI server in a new green thread."""
        eventlet.wsgi.server(socket, application, custom_pool=self.pool,
                             log=logging.WritableLogger(LOG))


class Middleware(object):
    """Base WSGI middleware wrapper.

    These classes require an application to be initialized that will be called
    next.  By default the middleware will simply call its wrapped app, or you
    can override __call__ to customize its behavior.
    """

    @classmethod
    def factory(cls, global_config, **local_config):
        """Used for paste app factories in paste.deploy config files.

        Any local configuration (that is, values under the [filter:APPNAME]
        section of the paste config) will be passed into the `__init__` method
        as kwargs.

        A hypothetical configuration would look like:

            [filter:analytics]
            redis_host = 127.0.0.1
            paste.filter_factory = nova.api.analytics:Analytics.factory

        which would result in a call to the `Analytics` class as

            import nova.api.analytics
            analytics.Analytics(app_from_paste, redis_host='127.0.0.1')

        You could of course re-implement the `factory` method in subclasses,
        but using the kwarg passing it shouldn't be necessary.

        """
        def _factory(app):
            return cls(app, **local_config)
        return _factory

    def __init__(self, application):
        self.application = application

    def process_request(self, req):
        """Called on each request.

        If this returns None, the next application down the stack will be
        executed. If it returns a response then that response will be returned
        and execution will stop here.

        """
        return None

    def process_response(self, response):
        """Do whatever you'd like to the response."""
        return response

    @webob.dec.wsgify
    def __call__(self, req):
        response = self.process_request(req)
        if response:
            return response
        response = req.get_response(self.application)
        return self.process_response(response)


class Request(webob.Request):

    def best_match_content_type(self):
        """Determine the most acceptable content-type.

        Based on:
            1) URI extension (.json/.xml)
            2) Content-type header
            3) Accept* headers
        """
        # First lookup http request path
        parts = self.path.rsplit('.', 1)
        if len(parts) > 1:
            _format = parts[1]
            if _format in ['json', 'xml']:
                return 'application/{0}'.format(_format)

        #Then look up content header
        type_from_header = self.get_content_type()
        if type_from_header:
            return type_from_header
        ctypes = ['application/json', 'application/xml']

        #Finally search in Accept-* headers
        bm = self.accept.best_match(ctypes)
        return bm or 'application/json'

    def get_content_type(self):
        allowed_types = ("application/xml", "application/json")
        if "Content-Type" not in self.headers:
            LOG.debug(_("Missing Content-Type"))
            return None
        _type = self.content_type
        if _type in allowed_types:
            return _type
        return None

    def best_match_language(self):
        """Determines best available locale from the Accept-Language header.

        :returns: the best language match or None if the 'Accept-Language'
                  header was not available in the request.
        """
        if not self.accept_language:
            return None
        all_languages = gettextutils.get_available_languages('neutron')
        return self.accept_language.best_match(all_languages)

    @property
    def context(self):
        if 'neutron.context' not in self.environ:
            self.environ['neutron.context'] = context.get_admin_context()
        return self.environ['neutron.context']


class ActionDispatcher(object):
    """Maps method name to local methods through action name."""

    def dispatch(self, *args, **kwargs):
        """Find and call local method."""
        action = kwargs.pop('action', 'default')
        action_method = getattr(self, str(action), self.default)
        return action_method(*args, **kwargs)

    def default(self, data):
        raise NotImplementedError()


class DictSerializer(ActionDispatcher):
    """Default request body serialization."""

    def serialize(self, data, action='default'):
        return self.dispatch(data, action=action)

    def default(self, data):
        return ""


class JSONDictSerializer(DictSerializer):
    """Default JSON request body serialization."""

    def default(self, data):
        def sanitizer(obj):
            return unicode(obj)
        return jsonutils.dumps(data, default=sanitizer)


class XMLDictSerializer(DictSerializer):

    def __init__(self, metadata=None, xmlns=None):
        """Object initialization.

        :param metadata: information needed to deserialize xml into
                         a dictionary.
        :param xmlns: XML namespace to include with serialized xml
        """
        super(XMLDictSerializer, self).__init__()
        self.metadata = metadata or {}
        if not xmlns:
            xmlns = self.metadata.get('xmlns')
        if not xmlns:
            xmlns = constants.XML_NS_V20
        self.xmlns = xmlns

    def default(self, data):
        """Return data as XML string.

        :param data: expect data to contain a single key as XML root, or
                     contain another '*_links' key as atom links. Other
                     case will use 'VIRTUAL_ROOT_KEY' as XML root.
        """
        try:
            links = None
            has_atom = False
            if data is None:
                root_key = constants.VIRTUAL_ROOT_KEY
                root_value = None
            else:
                link_keys = [k for k in data.iterkeys() or []
                             if k.endswith('_links')]
                if link_keys:
                    links = data.pop(link_keys[0], None)
                    has_atom = True
                root_key = (len(data) == 1 and
                            data.keys()[0] or constants.VIRTUAL_ROOT_KEY)
                root_value = data.get(root_key, data)
            doc = etree.Element("_temp_root")
            used_prefixes = []
            self._to_xml_node(doc, self.metadata, root_key,
                              root_value, used_prefixes)
            if links:
                self._create_link_nodes(list(doc)[0], links)
            return self.to_xml_string(list(doc)[0], used_prefixes, has_atom)
        except AttributeError as e:
            LOG.exception(str(e))
            return ''

    def __call__(self, data):
        # Provides a migration path to a cleaner WSGI layer, this
        # "default" stuff and extreme extensibility isn't being used
        # like originally intended
        return self.default(data)

    def to_xml_string(self, node, used_prefixes, has_atom=False):
        self._add_xmlns(node, used_prefixes, has_atom)
        return etree.tostring(node, encoding='UTF-8')

    #NOTE (ameade): the has_atom should be removed after all of the
    # xml serializers and view builders have been updated to the current
    # spec that required all responses include the xmlns:atom, the has_atom
    # flag is to prevent current tests from breaking
    def _add_xmlns(self, node, used_prefixes, has_atom=False):
        node.set('xmlns', self.xmlns)
        node.set(constants.TYPE_XMLNS, self.xmlns)
        if has_atom:
            node.set(constants.ATOM_XMLNS, constants.ATOM_NAMESPACE)
        node.set(constants.XSI_NIL_ATTR, constants.XSI_NAMESPACE)
        ext_ns = self.metadata.get(constants.EXT_NS, {})
        ext_ns_bc = self.metadata.get(constants.EXT_NS_COMP, {})
        for prefix in used_prefixes:
            if prefix in ext_ns:
                node.set('xmlns:' + prefix, ext_ns[prefix])
            if prefix in ext_ns_bc:
                node.set('xmlns:' + prefix, ext_ns_bc[prefix])

    def _to_xml_node(self, parent, metadata, nodename, data, used_prefixes):
        """Recursive method to convert data members to XML nodes."""
        result = etree.SubElement(parent, nodename)
        if ":" in nodename:
            used_prefixes.append(nodename.split(":", 1)[0])
        #TODO(bcwaldon): accomplish this without a type-check
        if isinstance(data, list):
            if not data:
                result.set(
                    constants.TYPE_ATTR,
                    constants.TYPE_LIST)
                return result
            singular = metadata.get('plurals', {}).get(nodename, None)
            if singular is None:
                if nodename.endswith('s'):
                    singular = nodename[:-1]
                else:
                    singular = 'item'
            for item in data:
                self._to_xml_node(result, metadata, singular, item,
                                  used_prefixes)
        #TODO(bcwaldon): accomplish this without a type-check
        elif isinstance(data, dict):
            if not data:
                result.set(
                    constants.TYPE_ATTR,
                    constants.TYPE_DICT)
                return result
            attrs = metadata.get('attributes', {}).get(nodename, {})
            for k, v in data.items():
                if k in attrs:
                    result.set(k, str(v))
                else:
                    self._to_xml_node(result, metadata, k, v,
                                      used_prefixes)
        elif data is None:
            result.set(constants.XSI_ATTR, 'true')
        else:
            if isinstance(data, bool):
                result.set(
                    constants.TYPE_ATTR,
                    constants.TYPE_BOOL)
            elif isinstance(data, int):
                result.set(
                    constants.TYPE_ATTR,
                    constants.TYPE_INT)
            elif isinstance(data, long):
                result.set(
                    constants.TYPE_ATTR,
                    constants.TYPE_LONG)
            elif isinstance(data, float):
                result.set(
                    constants.TYPE_ATTR,
                    constants.TYPE_FLOAT)
            LOG.debug(_("Data %(data)s type is %(type)s"),
                      {'data': data,
                       'type': type(data)})
            if isinstance(data, str):
                result.text = unicode(data, 'utf-8')
            else:
                result.text = unicode(data)
        return result

    def _create_link_nodes(self, xml_doc, links):
        for link in links:
            link_node = etree.SubElement(xml_doc, 'atom:link')
            link_node.set('rel', link['rel'])
            link_node.set('href', link['href'])


class ResponseHeaderSerializer(ActionDispatcher):
    """Default response headers serialization."""

    def serialize(self, response, data, action):
        self.dispatch(response, data, action=action)

    def default(self, response, data):
        response.status_int = 200


class ResponseSerializer(object):
    """Encode the necessary pieces into a response object."""

    def __init__(self, body_serializers=None, headers_serializer=None):
        self.body_serializers = {
            'application/xml': XMLDictSerializer(),
            'application/json': JSONDictSerializer(),
        }
        self.body_serializers.update(body_serializers or {})

        self.headers_serializer = (headers_serializer or
                                   ResponseHeaderSerializer())

    def serialize(self, response_data, content_type, action='default'):
        """Serialize a dict into a string and wrap in a wsgi.Request object.

        :param response_data: dict produced by the Controller
        :param content_type: expected mimetype of serialized response body

        """
        response = webob.Response()
        self.serialize_headers(response, response_data, action)
        self.serialize_body(response, response_data, content_type, action)
        return response

    def serialize_headers(self, response, data, action):
        self.headers_serializer.serialize(response, data, action)

    def serialize_body(self, response, data, content_type, action):
        response.headers['Content-Type'] = content_type
        if data is not None:
            serializer = self.get_body_serializer(content_type)
            response.body = serializer.serialize(data, action)

    def get_body_serializer(self, content_type):
        try:
            return self.body_serializers[content_type]
        except (KeyError, TypeError):
            raise exception.InvalidContentType(content_type=content_type)


class TextDeserializer(ActionDispatcher):
    """Default request body deserialization."""

    def deserialize(self, datastring, action='default'):
        return self.dispatch(datastring, action=action)

    def default(self, datastring):
        return {}


class JSONDeserializer(TextDeserializer):

    def _from_json(self, datastring):
        try:
            return jsonutils.loads(datastring)
        except ValueError:
            msg = _("Cannot understand JSON")
            raise exception.MalformedRequestBody(reason=msg)

    def default(self, datastring):
        return {'body': self._from_json(datastring)}


class ProtectedXMLParser(etree.XMLParser):
    def __init__(self, *args, **kwargs):
        etree.XMLParser.__init__(self, *args, **kwargs)
        self._parser.StartDoctypeDeclHandler = self.start_doctype_decl

    def start_doctype_decl(self, name, sysid, pubid, internal):
        raise ValueError(_("Inline DTD forbidden"))

    def doctype(self, name, pubid, system):
        raise ValueError(_("Inline DTD forbidden"))


class XMLDeserializer(TextDeserializer):

    def __init__(self, metadata=None):
        """Object initialization.

        :param metadata: information needed to deserialize xml into
                         a dictionary.
        """
        super(XMLDeserializer, self).__init__()
        self.metadata = metadata or {}
        xmlns = self.metadata.get('xmlns')
        if not xmlns:
            xmlns = constants.XML_NS_V20
        self.xmlns = xmlns

    def _get_key(self, tag):
        tags = tag.split("}", 1)
        if len(tags) == 2:
            ns = tags[0][1:]
            bare_tag = tags[1]
            ext_ns = self.metadata.get(constants.EXT_NS, {})
            if ns == self.xmlns:
                return bare_tag
            for prefix, _ns in ext_ns.items():
                if ns == _ns:
                    return prefix + ":" + bare_tag
            ext_ns_bc = self.metadata.get(constants.EXT_NS_COMP, {})
            for prefix, _ns in ext_ns_bc.items():
                if ns == _ns:
                    return prefix + ":" + bare_tag
        else:
            return tag

    def _get_links(self, root_tag, node):
        link_nodes = node.findall(constants.ATOM_LINK_NOTATION)
        root_tag = self._get_key(node.tag)
        link_key = "%s_links" % root_tag
        link_list = []
        for link in link_nodes:
            link_list.append({'rel': link.get('rel'),
                              'href': link.get('href')})
            # Remove link node in order to avoid link node process as
            # an item in _from_xml_node
            node.remove(link)
        return link_list and {link_key: link_list} or {}

    def _parseXML(self, text):
        parser = ProtectedXMLParser()
        parser.feed(text)
        return parser.close()

    def _from_xml(self, datastring):
        if datastring is None:
            return None
        plurals = set(self.metadata.get('plurals', {}))
        try:
            node = self._parseXML(datastring)
            root_tag = self._get_key(node.tag)
            # Deserialize link node was needed by unit test for verifying
            # the request's response
            links = self._get_links(root_tag, node)
            result = self._from_xml_node(node, plurals)
            # root_tag = constants.VIRTUAL_ROOT_KEY and links is not None
            # is not possible because of the way data are serialized.
            if root_tag == constants.VIRTUAL_ROOT_KEY:
                return result
            return dict({root_tag: result}, **links)
        except Exception as e:
            with excutils.save_and_reraise_exception():
                parseError = False
                # Python2.7
                if (hasattr(etree, 'ParseError') and
                    isinstance(e, getattr(etree, 'ParseError'))):
                    parseError = True
                # Python2.6
                elif isinstance(e, expat.ExpatError):
                    parseError = True
                if parseError:
                    msg = _("Cannot understand XML")
                    raise exception.MalformedRequestBody(reason=msg)

    def _from_xml_node(self, node, listnames):
        """Convert a minidom node to a simple Python type.

        :param listnames: list of XML node names whose subnodes should
                          be considered list items.

        """
        attrNil = node.get(str(etree.QName(constants.XSI_NAMESPACE, "nil")))
        attrType = node.get(str(etree.QName(
            self.metadata.get('xmlns'), "type")))
        if (attrNil and attrNil.lower() == 'true'):
            return None
        elif not len(node) and not node.text:
            if (attrType and attrType == constants.TYPE_DICT):
                return {}
            elif (attrType and attrType == constants.TYPE_LIST):
                return []
            else:
                return ''
        elif (len(node) == 0 and node.text):
            converters = {constants.TYPE_BOOL:
                          lambda x: x.lower() == 'true',
                          constants.TYPE_INT:
                          lambda x: int(x),
                          constants.TYPE_LONG:
                          lambda x: long(x),
                          constants.TYPE_FLOAT:
                          lambda x: float(x)}
            if attrType and attrType in converters:
                return converters[attrType](node.text)
            else:
                return node.text
        elif self._get_key(node.tag) in listnames:
            return [self._from_xml_node(n, listnames) for n in node]
        else:
            result = dict()
            for attr in node.keys():
                if (attr == 'xmlns' or
                    attr.startswith('xmlns:') or
                    attr == constants.XSI_ATTR or
                    attr == constants.TYPE_ATTR):
                    continue
                result[self._get_key(attr)] = node.get(attr)
            children = list(node)
            for child in children:
                result[self._get_key(child.tag)] = self._from_xml_node(
                    child, listnames)
            return result

    def default(self, datastring):
        return {'body': self._from_xml(datastring)}

    def __call__(self, datastring):
        # Adding a migration path to allow us to remove unncessary classes
        return self.default(datastring)


class RequestHeadersDeserializer(ActionDispatcher):
    """Default request headers deserializer."""

    def deserialize(self, request, action):
        return self.dispatch(request, action=action)

    def default(self, request):
        return {}


class RequestDeserializer(object):
    """Break up a Request object into more useful pieces."""

    def __init__(self, body_deserializers=None, headers_deserializer=None):
        self.body_deserializers = {
            'application/xml': XMLDeserializer(),
            'application/json': JSONDeserializer(),
        }
        self.body_deserializers.update(body_deserializers or {})

        self.headers_deserializer = (headers_deserializer or
                                     RequestHeadersDeserializer())

    def deserialize(self, request):
        """Extract necessary pieces of the request.

        :param request: Request object
        :returns tuple of expected controller action name, dictionary of
                 keyword arguments to pass to the controller, the expected
                 content type of the response

        """
        action_args = self.get_action_args(request.environ)
        action = action_args.pop('action', None)

        action_args.update(self.deserialize_headers(request, action))
        action_args.update(self.deserialize_body(request, action))

        accept = self.get_expected_content_type(request)

        return (action, action_args, accept)

    def deserialize_headers(self, request, action):
        return self.headers_deserializer.deserialize(request, action)

    def deserialize_body(self, request, action):
        try:
            content_type = request.best_match_content_type()
        except exception.InvalidContentType:
            LOG.debug(_("Unrecognized Content-Type provided in request"))
            return {}

        if content_type is None:
            LOG.debug(_("No Content-Type provided in request"))
            return {}

        if not len(request.body) > 0:
            LOG.debug(_("Empty body provided in request"))
            return {}

        try:
            deserializer = self.get_body_deserializer(content_type)
        except exception.InvalidContentType:
            with excutils.save_and_reraise_exception():
                LOG.debug(_("Unable to deserialize body as provided "
                            "Content-Type"))

        return deserializer.deserialize(request.body, action)

    def get_body_deserializer(self, content_type):
        try:
            return self.body_deserializers[content_type]
        except (KeyError, TypeError):
            raise exception.InvalidContentType(content_type=content_type)

    def get_expected_content_type(self, request):
        return request.best_match_content_type()

    def get_action_args(self, request_environment):
        """Parse dictionary created by routes library."""
        try:
            args = request_environment['wsgiorg.routing_args'][1].copy()
        except Exception:
            return {}

        try:
            del args['controller']
        except KeyError:
            pass

        try:
            del args['format']
        except KeyError:
            pass

        return args


class Application(object):
    """Base WSGI application wrapper. Subclasses need to implement __call__."""

    @classmethod
    def factory(cls, global_config, **local_config):
        """Used for paste app factories in paste.deploy config files.

        Any local configuration (that is, values under the [app:APPNAME]
        section of the paste config) will be passed into the `__init__` method
        as kwargs.

        A hypothetical configuration would look like:

            [app:wadl]
            latest_version = 1.3
            paste.app_factory = nova.api.fancy_api:Wadl.factory

        which would result in a call to the `Wadl` class as

            import neutron.api.fancy_api
            fancy_api.Wadl(latest_version='1.3')

        You could of course re-implement the `factory` method in subclasses,
        but using the kwarg passing it shouldn't be necessary.

        """
        return cls(**local_config)

    def __call__(self, environ, start_response):
        r"""Subclasses will probably want to implement __call__ like this:

        @webob.dec.wsgify(RequestClass=Request)
        def __call__(self, req):
          # Any of the following objects work as responses:

          # Option 1: simple string
          res = 'message\n'

          # Option 2: a nicely formatted HTTP exception page
          res = exc.HTTPForbidden(explanation='Nice try')

          # Option 3: a webob Response object (in case you need to play with
          # headers, or you want to be treated like an iterable, or or or)
          res = Response();
          res.app_iter = open('somefile')

          # Option 4: any wsgi app to be run next
          res = self.application

          # Option 5: you can get a Response object for a wsgi app, too, to
          # play with headers etc
          res = req.get_response(self.application)

          # You can then just return your response...
          return res
          # ... or set req.response and return None.
          req.response = res

        See the end of http://pythonpaste.org/webob/modules/dec.html
        for more info.

        """
        raise NotImplementedError(_('You must implement __call__'))


class Debug(Middleware):
    """Middleware for debugging.

    Helper class that can be inserted into any WSGI application chain
    to get information about the request and response.
    """

    @webob.dec.wsgify
    def __call__(self, req):
        print(("*" * 40) + " REQUEST ENVIRON")
        for key, value in req.environ.items():
            print(key, "=", value)
        print()
        resp = req.get_response(self.application)

        print(("*" * 40) + " RESPONSE HEADERS")
        for (key, value) in resp.headers.iteritems():
            print(key, "=", value)
        print()

        resp.app_iter = self.print_generator(resp.app_iter)

        return resp

    @staticmethod
    def print_generator(app_iter):
        """Print contents of a wrapper string iterator when iterated."""
        print(("*" * 40) + " BODY")
        for part in app_iter:
            sys.stdout.write(part)
            sys.stdout.flush()
            yield part
        print()


class Router(object):
    """WSGI middleware that maps incoming requests to WSGI apps."""

    @classmethod
    def factory(cls, global_config, **local_config):
        """Return an instance of the WSGI Router class."""
        return cls()

    def __init__(self, mapper):
        """Create a router for the given routes.Mapper.

        Each route in `mapper` must specify a 'controller', which is a
        WSGI app to call.  You'll probably want to specify an 'action' as
        well and have your controller be a wsgi.Controller, who will route
        the request to the action method.

        Examples:
          mapper = routes.Mapper()
          sc = ServerController()

          # Explicit mapping of one route to a controller+action
          mapper.connect(None, "/svrlist", controller=sc, action="list")

          # Actions are all implicitly defined
          mapper.resource("network", "networks", controller=nc)

          # Pointing to an arbitrary WSGI app.  You can specify the
          # {path_info:.*} parameter so the target app can be handed just that
          # section of the URL.
          mapper.connect(None, "/v1.0/{path_info:.*}", controller=BlogApp())
        """
        self.map = mapper
        self._router = routes.middleware.RoutesMiddleware(self._dispatch,
                                                          self.map)

    @webob.dec.wsgify
    def __call__(self, req):
        """Route the incoming request to a controller based on self.map.

        If no match, return a 404.
        """
        return self._router

    @staticmethod
    @webob.dec.wsgify(RequestClass=Request)
    def _dispatch(req):
        """Dispatch a Request.

        Called by self._router after matching the incoming request to a route
        and putting the information into req.environ. Either returns 404
        or the routed WSGI app's response.
        """
        match = req.environ['wsgiorg.routing_args'][1]
        if not match:
            language = req.best_match_language()
            msg = _('The resource could not be found.')
            msg = gettextutils.translate(msg, language)
            return webob.exc.HTTPNotFound(explanation=msg)
        app = match['controller']
        return app


class Resource(Application):
    """WSGI app that handles (de)serialization and controller dispatch.

    WSGI app that reads routing information supplied by RoutesMiddleware
    and calls the requested action method upon its controller.  All
    controller action methods must accept a 'req' argument, which is the
    incoming wsgi.Request. If the operation is a PUT or POST, the controller
    method must also accept a 'body' argument (the deserialized request body).
    They may raise a webob.exc exception or return a dict, which will be
    serialized by requested content type.

    """

    def __init__(self, controller, fault_body_function,
                 deserializer=None, serializer=None):
        """Object initialization.

        :param controller: object that implement methods created by routes lib
        :param deserializer: object that can serialize the output of a
                             controller into a webob response
        :param serializer: object that can deserialize a webob request
                           into necessary pieces
        :param fault_body_function: a function that will build the response
                                    body for HTTP errors raised by operations
                                    on this resource object

        """
        self.controller = controller
        self.deserializer = deserializer or RequestDeserializer()
        self.serializer = serializer or ResponseSerializer()
        self._fault_body_function = fault_body_function
        # use serializer's xmlns for populating Fault generator xmlns
        xml_serializer = self.serializer.body_serializers['application/xml']
        if hasattr(xml_serializer, 'xmlns'):
            self._xmlns = xml_serializer.xmlns

    @webob.dec.wsgify(RequestClass=Request)
    def __call__(self, request):
        """WSGI method that controls (de)serialization and method dispatch."""

        LOG.info(_("%(method)s %(url)s"), {"method": request.method,
                                           "url": request.url})

        try:
            action, args, accept = self.deserializer.deserialize(request)
        except exception.InvalidContentType:
            msg = _("Unsupported Content-Type")
            LOG.exception(_("InvalidContentType: %s"), msg)
            return Fault(webob.exc.HTTPBadRequest(explanation=msg),
                         self._xmlns)
        except exception.MalformedRequestBody:
            msg = _("Malformed request body")
            LOG.exception(_("MalformedRequestBody: %s"), msg)
            return Fault(webob.exc.HTTPBadRequest(explanation=msg),
                         self._xmlns)

        try:
            action_result = self.dispatch(request, action, args)
        except webob.exc.HTTPException as ex:
            LOG.info(_("HTTP exception thrown: %s"), unicode(ex))
            action_result = Fault(ex,
                                  self._xmlns,
                                  self._fault_body_function)
        except Exception:
            LOG.exception(_("Internal error"))
            # Do not include the traceback to avoid returning it to clients.
            action_result = Fault(webob.exc.HTTPServerError(),
                                  self._xmlns,
                                  self._fault_body_function)

        if isinstance(action_result, dict) or action_result is None:
            response = self.serializer.serialize(action_result,
                                                 accept,
                                                 action=action)
        else:
            response = action_result

        try:
            msg_dict = dict(url=request.url, status=response.status_int)
            msg = _("%(url)s returned with HTTP %(status)d") % msg_dict
        except AttributeError as e:
            msg_dict = dict(url=request.url, exception=e)
            msg = _("%(url)s returned a fault: %(exception)s") % msg_dict

        LOG.info(msg)

        return response

    def dispatch(self, request, action, action_args):
        """Find action-spefic method on controller and call it."""

        controller_method = getattr(self.controller, action)
        try:
            #NOTE(salvatore-orlando): the controller method must have
            # an argument whose name is 'request'
            return controller_method(request=request, **action_args)
        except TypeError as exc:
            LOG.exception(exc)
            return Fault(webob.exc.HTTPBadRequest(),
                         self._xmlns)


def _default_body_function(wrapped_exc):
    code = wrapped_exc.status_int
    fault_data = {
        'Error': {
            'code': code,
            'message': wrapped_exc.explanation}}
    # 'code' is an attribute on the fault tag itself
    metadata = {'attributes': {'Error': 'code'}}
    return fault_data, metadata


class Fault(webob.exc.HTTPException):
    """Generates an HTTP response from a webob HTTP exception."""

    def __init__(self, exception, xmlns=None, body_function=None):
        """Creates a Fault for the given webob.exc.exception."""
        self.wrapped_exc = exception
        self.status_int = self.wrapped_exc.status_int
        self._xmlns = xmlns
        self._body_function = body_function or _default_body_function

    @webob.dec.wsgify(RequestClass=Request)
    def __call__(self, req):
        """Generate a WSGI response based on the exception passed to ctor."""
        # Replace the body with fault details.
        fault_data, metadata = self._body_function(self.wrapped_exc)
        xml_serializer = XMLDictSerializer(metadata, self._xmlns)
        content_type = req.best_match_content_type()
        serializer = {
            'application/xml': xml_serializer,
            'application/json': JSONDictSerializer(),
        }[content_type]

        self.wrapped_exc.body = serializer.serialize(fault_data)
        self.wrapped_exc.content_type = content_type
        return self.wrapped_exc


# NOTE(salvatore-orlando): this class will go once the
# extension API framework is updated
class Controller(object):
    """WSGI app that dispatched to methods.

    WSGI app that reads routing information supplied by RoutesMiddleware
    and calls the requested action method upon itself.  All action methods
    must, in addition to their normal parameters, accept a 'req' argument
    which is the incoming wsgi.Request.  They raise a webob.exc exception,
    or return a dict which will be serialized by requested content type.

    """

    @webob.dec.wsgify(RequestClass=Request)
    def __call__(self, req):
        """Call the method specified in req.environ by RoutesMiddleware."""
        arg_dict = req.environ['wsgiorg.routing_args'][1]
        action = arg_dict['action']
        method = getattr(self, action)
        del arg_dict['controller']
        del arg_dict['action']
        if 'format' in arg_dict:
            del arg_dict['format']
        arg_dict['request'] = req
        result = method(**arg_dict)

        if isinstance(result, dict) or result is None:
            if result is None:
                status = 204
                content_type = ''
                body = None
            else:
                status = 200
                content_type = req.best_match_content_type()
                default_xmlns = self.get_default_xmlns(req)
                body = self._serialize(result, content_type, default_xmlns)

            response = webob.Response(status=status,
                                      content_type=content_type,
                                      body=body)
            msg_dict = dict(url=req.url, status=response.status_int)
            msg = _("%(url)s returned with HTTP %(status)d") % msg_dict
            LOG.debug(msg)
            return response
        else:
            return result

    def _serialize(self, data, content_type, default_xmlns):
        """Serialize the given dict to the provided content_type.

        Uses self._serialization_metadata if it exists, which is a dict mapping
        MIME types to information needed to serialize to that type.

        """
        _metadata = getattr(type(self), '_serialization_metadata', {})

        serializer = Serializer(_metadata, default_xmlns)
        try:
            return serializer.serialize(data, content_type)
        except exception.InvalidContentType:
            msg = _('The requested content type %s is invalid.') % content_type
            raise webob.exc.HTTPNotAcceptable(msg)

    def _deserialize(self, data, content_type):
        """Deserialize the request body to the specefied content type.

        Uses self._serialization_metadata if it exists, which is a dict mapping
        MIME types to information needed to serialize to that type.

        """
        _metadata = getattr(type(self), '_serialization_metadata', {})
        serializer = Serializer(_metadata)
        return serializer.deserialize(data, content_type)['body']

    def get_default_xmlns(self, req):
        """Provide the XML namespace to use if none is otherwise specified."""
        return None


# NOTE(salvatore-orlando): this class will go once the
# extension API framework is updated
class Serializer(object):
    """Serializes and deserializes dictionaries to certain MIME types."""

    def __init__(self, metadata=None, default_xmlns=None):
        """Create a serializer based on the given WSGI environment.

        'metadata' is an optional dict mapping MIME types to information
        needed to serialize a dictionary to that type.

        """
        self.metadata = metadata or {}
        self.default_xmlns = default_xmlns

    def _get_serialize_handler(self, content_type):
        handlers = {
            'application/json': JSONDictSerializer(),
            'application/xml': XMLDictSerializer(self.metadata),
        }

        try:
            return handlers[content_type]
        except Exception:
            raise exception.InvalidContentType(content_type=content_type)

    def serialize(self, data, content_type):
        """Serialize a dictionary into the specified content type."""
        return self._get_serialize_handler(content_type).serialize(data)

    def deserialize(self, datastring, content_type):
        """Deserialize a string to a dictionary.

        The string must be in the format of a supported MIME type.

        """
        try:
            return self.get_deserialize_handler(content_type).deserialize(
                datastring)
        except Exception:
            raise webob.exc.HTTPBadRequest(_("Could not deserialize data"))

    def get_deserialize_handler(self, content_type):
        handlers = {
            'application/json': JSONDeserializer(),
            'application/xml': XMLDeserializer(self.metadata),
        }

        try:
            return handlers[content_type]
        except Exception:
            raise exception.InvalidContentType(content_type=content_type)
