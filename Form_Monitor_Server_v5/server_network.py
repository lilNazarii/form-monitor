"""Public HTTPS transport pinned to a validated IP, including browser requests."""
import base64
import http.client
import ipaddress
import os
import socket
import ssl
from urllib.parse import urlsplit, quote


def allowed_hosts():
    """Legacy owner-managed sites; new customers use account-bound verification."""
    return {h.strip().lower() for h in os.environ.get('FM_ALLOWED_FORM_HOSTS', '').split(',') if h.strip()}


def public_target(url):
    try:
        p = urlsplit(url)
        if (p.scheme != 'https' or not p.hostname or p.username or p.password
                or p.port not in (None, 443) or len(url) > 2000
                or any(c.isspace() or ord(c) < 32 for c in url) or '\\' in url):
            raise ValueError()
        host = p.hostname.encode('idna').decode('ascii').lower()
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        ips = list(dict.fromkeys(a[4][0] for a in addresses))
        if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
            raise ValueError()
    except (ValueError, UnicodeError, OSError):
        raise ValueError('Use a publicly reachable HTTPS website on port 443.') from None
    return p, host, ips[0]


def validate_url(url):
    public_target(url)
    return url


class PublicHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, address, timeout=10):
        super().__init__(host, timeout=timeout, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        # No second DNS lookup: a rebinding response cannot select an internal IP.
        raw = socket.create_connection((self.address, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def public_request(url, method='GET', headers=None, body=None, limit=2*1024*1024):
    p, host, address = public_target(url)
    headers = {k: v for k, v in (headers or {}).items() if k.lower() not in
               ('host', 'connection', 'accept-encoding', 'content-length', 'transfer-encoding', 'proxy-authorization')}
    headers.update({'Host': host, 'Accept-Encoding': 'identity', 'Connection': 'close'})
    connection = PublicHTTPSConnection(host, address)
    try:
        target = quote((p.path or '/') + ('?' + p.query if p.query else ''), safe="/%?=&;:+,@!$'()*-._~")
        connection.request(method, target, body=body, headers=headers)
        response = connection.getresponse()
        content = response.read(limit + 1)
        if len(content) > limit:
            raise ValueError('Website response is too large.')
        return response.status, dict(response.getheaders()), content
    except (OSError, http.client.HTTPException):
        raise ValueError('The website could not be reached securely. Check its HTTPS address and try again.') from None
    finally:
        connection.close()


def browser_routes(site_url):
    """Only submit to the verified host; external assets are read-only public HTTPS."""
    site_host = urlsplit(site_url).hostname.encode('idna').decode('ascii').lower()
    budget = {'requests': 0, 'bytes': 0}

    def route_request(route):
        try:
            request = route.request
            host = urlsplit(request.url).hostname
            host = host.encode('idna').decode('ascii').lower() if host else ''
            budget['requests'] += 1
            if budget['requests'] > 160 or budget['bytes'] > 60*1024*1024:
                raise ValueError('Website resource limit reached.')
            if request.method not in ('GET', 'HEAD', 'OPTIONS', 'POST'):
                raise ValueError('Unsupported request method.')
            if host != site_host and (request.method not in ('GET', 'HEAD') or request.is_navigation_request()):
                raise ValueError('Form submissions and navigation must stay on the verified website.')
            body = request.post_data_buffer
            if body and len(body) > 256*1024:
                raise ValueError('Form submission is too large.')
            headers = request.all_headers()
            if host == site_host and host.endswith('.localsite.io') and os.environ.get('LIVE_LINK_USER'):
                credentials = os.environ['LIVE_LINK_USER'] + ':' + os.environ.get('LIVE_LINK_PASSWORD', '')
                headers['Authorization'] = 'Basic ' + base64.b64encode(credentials.encode()).decode()
            status, response_headers, content = public_request(request.url, request.method, headers, body, limit=10*1024*1024)
            budget['bytes'] += len(content)
            response_headers = {k: v for k, v in response_headers.items() if k.lower() not in
                                ('connection', 'transfer-encoding', 'content-length')}
            route.fulfill(status=status, headers=response_headers, body=content)
        except (ValueError, OSError):
            route.abort('blockedbyclient')
    return route_request
