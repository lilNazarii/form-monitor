"""Private-beta network policy: administrator-approved public HTTPS origins only."""
import ipaddress
import os
import socket
from urllib.parse import urlsplit

def allowed_hosts():
    return {h.strip().lower() for h in os.environ.get('FM_ALLOWED_FORM_HOSTS','').split(',') if h.strip()}

def validate_url(url):
    p=urlsplit(url)
    if p.scheme!='https' or not p.hostname or p.username or p.password or p.port not in (None,443):
        raise ValueError('Use a public HTTPS form URL on port 443.')
    host=p.hostname.lower()
    if host not in allowed_hosts():raise ValueError('This website is not approved for this server. Ask the owner to add its hostname.')
    addresses=socket.getaddrinfo(host,443,type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError('Only public internet addresses are allowed.')
    return url

def browser_route(route):
    # Every browser request, including redirects and subresources, follows the same policy.
    try:validate_url(route.request.url)
    except (ValueError,OSError):route.abort('blockedbyclient')
    else:route.continue_()
