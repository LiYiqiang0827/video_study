#!/usr/bin/env python3
"""System curl media transport used by the explicitly frozen batch profile."""
import io
import os
import re
import subprocess
import sys
from email.parser import BytesHeaderParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

CURL = '/usr/bin/curl'
RETRY_OVERRIDE = {'effective_per_request_extra_retries': 6, 'effective_total_extra_retries': 60}
import download_video as worker
from yt_dlp.networking.common import Response
from yt_dlp.networking.exceptions import CertificateVerifyError, HTTPError, RequestError, SSLError, TransportError
from yt_dlp.utils.networking import HTTPHeaderDict, select_proxy


def media_url(url):
    parsed = urlparse(url)
    host = (parsed.hostname or '').lower()
    return (parsed.scheme.lower() == 'https' and parsed.username is None and parsed.password is None
            and (host == 'googlevideo.com' or host.endswith('.googlevideo.com')))


def quoted(value):
    value = str(value)
    if any(c in value for c in '\r\n\0'):
        raise RequestError('system_curl_invalid_config_value')
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('\t', '\\t') + '"'


class CurlError(Exception):
    def __init__(self, code):
        self.code = int(code)
        super().__init__('system_curl_failure')


class SystemCurlTransport:
    RH_KEY = 'SystemCurl'
    RH_NAME = 'system_curl'

    def __init__(self, ydl):
        self.ydl = ydl
        self.requests = 0
        self.exit_codes = []

    def _config(self, request):
        if not media_url(request.url) or request.method not in {'GET', 'HEAD'} or request.data is not None:
            raise RequestError('system_curl_request_scope_invalid', handler=self)
        if self.ydl.params.get('nocheckcertificate') or self.ydl.params.get('legacyserverconnect'):
            raise RequestError('system_curl_verified_tls_required', handler=self)
        headers = HTTPHeaderDict(self.ydl.params.get('http_headers'), request.headers)
        headers['Accept-Encoding'] = 'identity'
        lines = ['silent', 'globoff', 'include', 'suppress-connect-headers',
                 'retry = 0', 'connect-timeout = 20', 'max-time = 295', 'proto = "=https"',
                 'url = ' + quoted(request.url)]
        if request.method == 'HEAD':
            lines.append('head')
        for name, value in headers.items():
            if (not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name)
                    or name.lower() in {'cookie', 'cookie2', 'authorization', 'proxy-authorization',
                                        'x-youtube-identity-token'}):
                raise RequestError('system_curl_credential_or_invalid_header', handler=self)
            lines.append('header = ' + quoted(f'{name}: {value}'))
        proxy = select_proxy(request.url, request.proxies or self.ydl.proxies)
        if proxy:
            parsed = urlparse(proxy)
            if (parsed.scheme not in {'http', 'https', 'socks5', 'socks5h'} or not parsed.hostname
                    or parsed.username is not None or parsed.password is not None):
                raise RequestError('system_curl_proxy_invalid', handler=self)
        # Explicit empty proxy prevents curl from silently consulting a different env proxy.
        lines.append('proxy = ' + quoted(proxy or ''))
        return ('\n'.join(lines) + '\n').encode('utf-8')

    def _response(self, payload, request):
        for _ in range(6):
            end = payload.find(b'\r\n\r\n')
            separator = 4
            if end < 0:
                end = payload.find(b'\n\n')
                separator = 2
            if end < 0 or end > 131072:
                raise TransportError('system_curl_invalid_response', handler=self)
            header, payload = payload[:end], payload[end + separator:]
            first, _, remaining = header.partition(b'\n')
            match = re.fullmatch(rb'HTTP/\S+ ([0-9]{3})(?: [^\r\n]*)?\r?', first)
            if match is None:
                raise TransportError('system_curl_invalid_response', handler=self)
            status = int(match[1])
            if 100 <= status < 200:
                continue
            headers = BytesHeaderParser().parsebytes(remaining + b'\r\n\r\n')
            return Response(io.BytesIO(payload), request.url, headers, status=status)
        raise TransportError('system_curl_invalid_response', handler=self)

    def send(self, request):
        current = request.copy()
        for redirect in range(6):
            config = self._config(current)
            self.requests += 1
            env = os.environ.copy()
            env.pop('SSLKEYLOGFILE', None)
            try:
                completed = subprocess.run([CURL, '--disable', '--config', '-'], input=config,
                                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                           timeout=300, check=False, env=env)
            except subprocess.TimeoutExpired:
                raise TransportError('system_curl_timeout', handler=self) from None
            except OSError:
                raise TransportError('system_curl_unavailable', handler=self) from None
            code = completed.returncode
            self.exit_codes.append(code)
            del self.exit_codes[:-20]
            if code:
                cause = CurlError(code)
                if code in {51, 58, 60, 77, 83, 90, 91}:
                    raise CertificateVerifyError('certificate verify failed', cause=cause, handler=self)
                if code == 35:
                    # The existing worker owns both per-request and shared TLS retry budgets.
                    raise SSLError('curl: (35) SSL connect failed', cause=cause, handler=self)
                raise TransportError('system_curl_transport_failed', cause=cause, handler=self)
            response = self._response(completed.stdout, current)
            if response.status in {301, 302, 303, 307, 308}:
                location = response.get_header('Location')
                response.close()
                if not location or redirect == 5:
                    raise RequestError('system_curl_redirect_rejected', handler=self)
                next_url = urljoin(current.url, location)
                if not media_url(next_url):
                    raise RequestError('system_curl_redirect_scope_invalid', handler=self)
                current = current.copy()
                current.url = next_url
                continue
            if not 200 <= response.status < 300:
                error = HTTPError(response)
                error.handler = self
                raise error
            if (response.get_header('Content-Encoding') or 'identity').strip().lower() != 'identity':
                response.close()
                raise TransportError('system_curl_unexpected_content_encoding', handler=self)
            return response
        raise RequestError('system_curl_redirect_rejected', handler=self)


TRANSPORTS = []


class SystemCurlYDL(worker.BoundedConnectionYDL):
    TLS_RETRY_BUDGET = RETRY_OVERRIDE['effective_total_extra_retries']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.params['tls_connection_retries'] = RETRY_OVERRIDE['effective_per_request_extra_retries']

    def build_request_director(self, handlers, preferences=None):
        director = super().build_request_director(handlers, preferences)
        original_send = director.send
        transport = SystemCurlTransport(self)
        TRANSPORTS.append(transport)

        def send(request):
            if media_url(request.url) and request.method in {'GET', 'HEAD'}:
                return transport.send(request)
            return original_send(request)

        director.send = send
        return director
