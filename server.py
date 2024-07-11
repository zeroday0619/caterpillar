#!/usr/bin/python3
#
# server.py
# server file with TCP connection mode
#
# Caterpillar Proxy - The simple web debugging proxy (formerly, php-httpproxy)
# Namyheon Go (Catswords Research) <gnh1201@gmail.com>
# https://github.com/gnh1201/caterpillar
# Created at: 2022-10-06
# Updated at: 2024-07-09
#

import argparse
import socket
import sys
import os
from _thread import *
from subprocess import PIPE, Popen
import base64
import json
import ssl
import time
import hashlib
import traceback
import textwrap
from datetime import datetime
from platform import python_version

import re
import requests
from requests.auth import HTTPBasicAuth
from urllib.parse import urlparse
from decouple import config

from base import Extension, extract_credentials, jsonrpc2_create_id, jsonrpc2_encode, jsonrpc2_result_encode, Logger

logger = Logger(name="server")

# initialization
try:
    listening_port = config('PORT', default=5555, cast=int)
    _username, _password, server_url = extract_credentials(config('SERVER_URL', default=''))
    server_connection_type = config('SERVER_CONNECTION_TYPE', default='')
    cakey = config('CA_KEY', default='ca.key')
    cacert = config('CA_CERT', default='ca.crt')
    certkey = config('CERT_KEY', default='cert.key')
    certdir = config('CERT_DIR', default='certs/')
    openssl_binpath = config('OPENSSL_BINPATH', default='openssl')
    client_encoding = config('CLIENT_ENCODING', default='utf-8')
    local_domain = config('LOCAL_DOMAIN', default='')
    proxy_pass = config('PROXY_PASS', default='')
    use_extensions = config('USE_EXTENSIONS', default='')
except KeyboardInterrupt:
    logger.warning("[*] User has requested an interrupt")
    logger.warning("[*] Application Exiting.....")
    sys.exit()
except Exception as e:
    logger.error("[*] Failed to initialize:", exc_info=e)

parser = argparse.ArgumentParser()
parser.add_argument('--max_conn', help="Maximum allowed connections", default=255, type=int)
parser.add_argument('--buffer_size', help="Number of samples to be used", default=8192, type=int)

args = parser.parse_args()
max_connection = args.max_conn
buffer_size = args.buffer_size
accepted_relay = {}
resolved_address_list = []

# set environment of Extension
Extension.set_buffer_size(buffer_size)
Extension.set_protocol('tcp')

# set basic authentication
auth = None
if _username:
    auth = HTTPBasicAuth(_username, _password)


def parse_first_data(data):
    parsed_data = (b'', b'', b'', b'', b'')

    try:
        first_line = data.split(b'\n')[0]

        method, url = first_line.split()[0:2]

        http_pos = url.find(b'://')  #Finding the position of ://
        scheme = b'http'  # check http/https or other protocol
        if http_pos == -1:
            temp = url
        else:
            temp = url[(http_pos + 3):]
            scheme = url[0:http_pos]

        port_pos = temp.find(b':')

        webserver_pos = temp.find(b'/')
        if webserver_pos == -1:
            webserver_pos = len(temp)
        webserver = b''
        port = -1
        if port_pos == -1 or webserver_pos < port_pos:
            port = 80
            webserver = temp[:webserver_pos]
        else:
            port = int((temp[(port_pos + 1):])[:webserver_pos - port_pos - 1])
            webserver = temp[:port_pos]
            if port == 443:
                scheme = b'https'

        parsed_data = (webserver, port, scheme, method, url)
    except Exception as e:
        logger.error("[*] Exception on parsing the header", exc_info=e)

    return parsed_data


def conn_string(conn, data, addr):
    # JSON-RPC 2.0 request
    def process_jsonrpc2(data):
        jsondata = json.loads(data.decode(client_encoding, errors='ignore'))
        if jsondata['jsonrpc'] == "2.0":
            jsonrpc2_server(conn, jsondata['id'], jsondata['method'], jsondata['params'])
            return True
        return False

    # JSON-RPC 2.0 request over Socket (stateful)
    if data.find(b'{') == 0 and process_jsonrpc2(data):
        # will be close by the client
        return

    # parse first data (header)
    webserver, port, scheme, method, url = parse_first_data(data)

    # JSON-RPC 2.0 request over HTTP (stateless)
    path = urlparse(url.decode(client_encoding)).path
    if path == "/proxy-cgi/jsonrpc2":
        conn.send(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n')
        pos = data.find(b'\r\n\r\n')
        if pos > -1 and process_jsonrpc2(data[pos + 4:]):
            conn.close()  # will be close by the server
            return

    # if it is reverse proxy
    if local_domain != '':
        localserver = local_domain.encode(client_encoding)
        if webserver == localserver or data.find(b'\nHost: ' + localserver) > -1:
            logger.info("[*] Detected the reverse proxy request: %s" % local_domain)
            scheme, _webserver, _port = proxy_pass.encode(client_encoding).split(b':')
            webserver = _webserver[2:]
            port = int(_port.decode(client_encoding))

    proxy_server(webserver, port, scheme, method, url, conn, addr, data)


def jsonrpc2_server(conn, id, method, params):
    if method == "relay_accept":
        accepted_relay[id] = conn
        connection_speed = params['connection_speed']
        logger.info("[*] connection speed: %s milliseconds" % (str(connection_speed)))
        while conn.fileno() > -1:
            time.sleep(1)
        del accepted_relay[id]
        logger.info("[*] relay destroyed: %s" % id)
    else:
        Extension.dispatch_rpcmethod(method, "call", id, params, conn)

    #return in conn_string()


def proxy_connect(webserver, conn):
    hostname = webserver.decode(client_encoding)
    certpath = "%s/%s.crt" % (certdir.rstrip('/'), hostname)

    if not os.path.exists(certdir):
        os.makedirs(certdir)

    # https://stackoverflow.com/questions/24055036/handle-https-request-in-proxy-server-by-c-sharp-connect-tunnel
    conn.send(b'HTTP/1.1 200 Connection Established\r\n\r\n')

    # https://github.com/inaz2/proxy2/blob/master/proxy2.py
    try:
        if not os.path.isfile(certpath):
            epoch = "%d" % (time.time() * 1000)
            p1 = Popen([openssl_binpath, "req", "-new", "-key", certkey, "-subj", "/CN=%s" % hostname], stdout=PIPE)
            p2 = Popen(
                [openssl_binpath, "x509", "-req", "-days", "3650", "-CA", cacert, "-CAkey", cakey, "-set_serial", epoch,
                 "-out", certpath], stdin=p1.stdout, stderr=PIPE)
            p2.communicate()
    except Exception as e:
        logger.error("[*] Skipped generating the certificate.", exc_info=e)

    # https://stackoverflow.com/questions/11255530/python-simple-ssl-socket-server
    # https://docs.python.org/3/library/ssl.html
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certpath, certkey)

    # https://stackoverflow.com/questions/11255530/python-simple-ssl-socket-server
    conn = context.wrap_socket(conn, server_side=True)
    data = conn.recv(buffer_size)

    return (conn, data)


def proxy_check_filtered(data, webserver, port, scheme, method, url):
    filtered = False

    filters = Extension.get_filters()
    logger.info("[*] Checking data with %s filters..." % (str(len(filters))))
    for f in filters:
        filtered = f.test(filtered, data, webserver, port, scheme, method, url)

    return filtered


def proxy_server(webserver, port, scheme, method, url, conn, addr, data):
    try:
        logger.info("[*] Started the request. %s" % (str(addr[0])))

        # SSL negotiation
        is_ssl = scheme in [b'https', b'tls', b'ssl']
        if is_ssl and method == b'CONNECT':
            while True:
                try:
                    conn, data = proxy_connect(webserver, conn)
                    break  # success
                #except OSError as e:
                #    print ("[*] Retrying SSL negotiation... (%s:%s) %s" % (webserver.decode(client_encoding), str(port), str(e)))
                except Exception as e:
                    raise Exception(
                        "SSL negotiation failed. (%s:%s) %s" % (webserver.decode(client_encoding), str(port), str(e)))

        # override data
        if is_ssl:
            _, _, _, method, url = parse_first_data(data)

        # https://stackoverflow.com/questions/44343739/python-sockets-ssl-eof-occurred-in-violation-of-protocol
        def sock_close(sock, is_ssl=False):
            #if is_ssl:
            #    sock = sock.unwrap()
            #sock.shutdown(socket.SHUT_RDWR)
            sock.close()

        # Wait to see if there is more data to transmit
        def sendall(sock, conn, data):
            # send first chuck
            if proxy_check_filtered(data, webserver, port, scheme, method, url):
                sock.close()
                raise Exception("Filtered request")
            sock.send(data)
            if len(data) < buffer_size:
                return

            # send following chunks
            buffered = b''
            conn.settimeout(1)
            while True:
                try:
                    chunk = conn.recv(buffer_size)
                    if not chunk:
                        break
                    buffered += chunk
                    if proxy_check_filtered(buffered, webserver, port, scheme, method, url):
                        sock_close(sock, is_ssl)
                        raise Exception("Filtered request")
                    sock.send(chunk)
                    if len(buffered) > buffer_size * 2:
                        buffered = buffered[-buffer_size * 2:]
                except:
                    break

        # localhost mode
        if server_url == "localhost":
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

            if is_ssl:
                context = ssl.create_default_context()
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE

                sock = context.wrap_socket(sock, server_hostname=webserver.decode(client_encoding))
                sock.connect((webserver, port))
                #sock.sendall(data)
                sendall(sock, conn, data)
            else:
                sock.connect((webserver, port))
                #sock.sendall(data)
                sendall(sock, conn, data)

            i = 0
            is_http_403 = False
            buffered = b''
            while True:
                chunk = sock.recv(buffer_size)
                if not chunk:
                    break
                if i == 0 and chunk.find(b'HTTP/1.1 403') == 0:
                    is_http_403 = True
                    break
                buffered += chunk
                if proxy_check_filtered(buffered, webserver, port, scheme, method, url):
                    sock_close(sock, is_ssl)
                    add_filtered_host(webserver.decode(client_encoding), '127.0.0.1')
                    raise Exception("Filtered response")
                conn.send(chunk)
                if len(buffered) > buffer_size * 2:
                    buffered = buffered[-buffer_size * 2:]
                i += 1

            # when blocked
            if is_http_403:
                logger.warning("[*] Blocked the request by remote server: %s" % (webserver.decode(client_encoding)))

                def bypass_callback(response, *args, **kwargs):
                    if response.status_code != 200:
                        conn.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n{\"status\":403}")
                        return

                    # https://stackoverflow.com/questions/20658572/python-requests-print-entire-http-request-raw
                    format_headers = lambda d: '\r\n'.join(f'{k}: {v}' for k, v in d.items())

                    first_data = textwrap.dedent('HTTP/1.1 {res.status_code} {res.reason}\r\n{reshdrs}\r\n\r\n').format(
                        res=response,
                        reshdrs=format_headers(response.headers),
                    ).encode(client_encoding)
                    conn.send(first_data)

                    for chunk in response.iter_content(chunk_size=buffer_size):
                        conn.send(chunk)

                if is_ssl and method == b'GET':
                    logger.info("[*] Trying to bypass blocked request...")
                    remote_url = "%s://%s%s" % (
                    scheme.decode(client_encoding), webserver.decode(client_encoding), url.decode(client_encoding))
                    requests.get(remote_url, stream=True, verify=False, hooks={'response': bypass_callback})
                else:
                    conn.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n{\"status\":403}")

            sock_close(sock, is_ssl)

            logger.info("[*] Received %s chunks. (%s bytes per chunk)" % (str(i), str(buffer_size)))

        # stateful mode
        elif server_connection_type == "stateful":
            proxy_data = {
                'headers': {
                    "User-Agent": "php-httpproxy/0.1.5 (Client; Python " + python_version() + "; abuse@catswords.net)",
                },
                'data': {
                    "buffer_size": str(buffer_size),
                    "client_address": str(addr[0]),
                    "client_port": str(listening_port),
                    "client_encoding": client_encoding,
                    "remote_address": webserver.decode(client_encoding),
                    "remote_port": str(port),
                    "scheme": scheme.decode(client_encoding),
                    "datetime": datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
                }
            }

            # get client address
            logger.info("[*] resolving the client address...")
            while len(resolved_address_list) == 0:
                try:
                    _, query_data = jsonrpc2_encode('get_client_address')
                    query = requests.post(server_url, headers=proxy_data['headers'], data=query_data, timeout=1,
                                          auth=auth)
                    if query.status_code == 200:
                        result = query.json()['result']
                        resolved_address_list.append(result['data'])
                    logger.info("[*] resolved IP: %s" % (result['data']))
                except requests.exceptions.ReadTimeout as e:
                    pass
            proxy_data['data']['client_address'] = resolved_address_list[0]

            # build a tunnel
            def relay_connect(id, raw_data, proxy_data):
                try:
                    # The tunnel connect forever until the client destroy it
                    relay = requests.post(server_url, headers=proxy_data['headers'], data=raw_data, stream=True,
                                          timeout=None, auth=auth)
                    for chunk in relay.iter_content(chunk_size=buffer_size):
                        jsondata = json.loads(chunk.decode(client_encoding, errors='ignore'))
                        if jsondata['jsonrpc'] == "2.0" and ("error" in jsondata):
                            e = jsondata['error']
                            logger.error("[*] Error received from the relay server: (%s) %s" % (
                            str(e['code']), str(e['message'])))
                except requests.exceptions.ReadTimeout as e:
                    pass

            id, raw_data = jsonrpc2_encode('relay_connect', proxy_data['data'])
            start_new_thread(relay_connect, (id, raw_data, proxy_data))

            # wait for the relay
            logger.info("[*] waiting for the relay... %s" % id)
            max_reties = 30
            t = 0
            while t < max_reties and not id in accepted_relay:
                time.sleep(1)
                t += 1
            if t < max_reties:
                sock = accepted_relay[id]
                logger.info("[*] connected the relay. %s" % id)
                sendall(sock, conn, data)
            else:
                resolved_address_list.remove(resolved_address_list[0])
                logger.info("[*] the relay is gone. %s" % id)
                sock_close(sock, is_ssl)
                return

            # get response
            i = 0
            buffered = b''
            while True:
                chunk = sock.recv(buffer_size)
                if not chunk:
                    break
                buffered += chunk
                if proxy_check_filtered(buffered, webserver, port, scheme, method, url):
                    sock_close(sock, is_ssl)
                    add_filtered_host(webserver.decode(client_encoding), '127.0.0.1')
                    raise Exception("Filtered response")
                conn.send(chunk)
                if len(buffered) > buffer_size * 2:
                    buffered = buffered[-buffer_size * 2:]
                i += 1

            sock_close(sock, is_ssl)

            logger.info("[*] Received %s chunks. (%s bytes per chunk)" % (str(i), str(buffer_size)))

        # stateless mode
        elif server_connection_type == "stateless":
            proxy_data = {
                'headers': {
                    "User-Agent": "php-httpproxy/0.1.5 (Client; Python " + python_version() + "; abuse@catswords.net)",
                },
                'data': {
                    "buffer_size": str(buffer_size),
                    "request_data": base64.b64encode(data).decode(client_encoding),
                    "request_length": str(len(data)),
                    "client_address": str(addr[0]),
                    "client_port": str(listening_port),
                    "client_encoding": client_encoding,
                    "remote_address": webserver.decode(client_encoding),
                    "remote_port": str(port),
                    "scheme": scheme.decode(client_encoding),
                    "datetime": datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
                }
            }
            _, raw_data = jsonrpc2_encode('relay_request', proxy_data['data'])

            logger.info("[*] Sending %s bytes..." % (str(len(raw_data))))

            i = 0
            relay = requests.post(server_url, headers=proxy_data['headers'], data=raw_data, stream=True, auth=auth)
            buffered = b''
            for chunk in relay.iter_content(chunk_size=buffer_size):
                buffered += chunk
                if proxy_check_filtered(buffered, webserver, port, scheme, method, url):
                    add_filtered_host(webserver.decode(client_encoding), '127.0.0.1')
                    raise Exception("Filtered response")
                conn.send(chunk)
                if len(buffered) > buffer_size * 2:
                    buffered = buffered[-buffer_size * 2:]
                i += 1

            logger.info("[*] Received %s chunks. (%s bytes per chunk)" % (str(i), str(buffer_size)))

        # nothing at all
        else:
            connector = Extension.get_connector(server_connection_type)
            if connector:
                connector.connect(conn, data, webserver, port, scheme, method, url)
            else:
                raise Exception("Unsupported connection type")

        logger.info("[*] Request and received. Done. %s" % (str(addr[0])))
        conn.close()
    except Exception as e:
        print(traceback.format_exc())
        logger.error("[*] Exception on requesting the data.", exc_info=e)
        conn.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n{\"status\":403}")
        conn.close()


# journaling a filtered hosts
def add_filtered_host(domain, ip_address):
    hosts_path = './filtered.hosts'
    with open(hosts_path, 'r') as file:
        lines = file.readlines()

    domain_exists = any(domain in line for line in lines)
    if not domain_exists:
        lines.append(f"{ip_address}\t{domain}\n")
        with open(hosts_path, 'w') as file:
            file.writelines(lines)


def start():  #Main Program
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(('', listening_port))
        sock.listen(max_connection)
        logger.info("[*] Server started successfully [ %d ]" % listening_port)
    except Exception as e:
        logger.error("[*] Unable to Initialize Socket", exc_info=e)
        sys.exit(2)

    while True:
        try:
            conn, addr = sock.accept()  #Accept connection from client browser
            data = conn.recv(buffer_size)  #Recieve client data
            start_new_thread(conn_string, (conn, data, addr))  #Starting a thread
        except KeyboardInterrupt:
            sock.close()
            logger.info("[*] Graceful Shutdown")
            sys.exit(1)


if __name__ == "__main__":
    # Fix Value error
    if use_extensions:
        # load extensions
        for s in use_extensions.split(','):
            Extension.register(s)
    else:
        logger.warning("[*] No extensions registered")

    # start Caterpillar
    start()
