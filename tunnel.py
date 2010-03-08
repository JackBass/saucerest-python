#! /usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright (c) 2009-2010 Sauce Labs Inc
#
# Permission is hereby granted, free of charge, to any person obtaining
# a copy of this software and associated documentation files (the
# 'Software'), to deal in the Software without restriction, including
# without limitation the rights to use, copy, modify, merge, publish,
# distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so, subject to
# the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED 'AS IS', WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
# IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY
# CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT,
# TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
# SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

import sys
import re
import socket
import logging
from optparse import OptionParser

import daemon
from twisted.internet import reactor

import saucerest
import sshtunnel
from tunnelmonitor import get_new_tunnel, heartbeat

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

tunnel_id = None


def _parse_options():
    op = OptionParser(
            usage="Usage: %prog [options] <username> <access key> <local host>"
                  " <local port>:<remote port>[,<local port>:<remote port>]"
                  " <remote domain>[,<remote domain>...]")
    op.add_option("-d", "--daemonize", default=False, action='store_true',
                  help="background the process once the tunnel is established")
    op.add_option("-p", "--pidfile", default="tunnel.pid",
                  help="when used with --daemonize, write backgrounded PID "
                       "to PIDFILE [default: %default]")
    op.add_option("-r", "--readyfile",
                  help="create READYFILE when the tunnel is ready")
    op.add_option("-l", "--logfile",
                  help="write messages to LOGFILE (use with -d or for"
                       " debugging)")
    op.add_option("-s", "--shutdown", default=False, action='store_true',
                  help="shutdown any existing tunnel machines using one or more"
                       " requested domain names")
    op.add_option("--diagnostic", default=False, action='store_true',
                  help="using this option, we will run a set of tests to make"
                       " sure the arguments given are correct. If all works,"
                       " will open the tunnels in debug mode")
    op.add_option("-b", "--baseurl", dest="base_url",
                  default="https://saucelabs.com",
                  help="use an alternate base URL for the saucelabs service")

    options, args = op.parse_args()

    num_missing = 5 - len(args)
    if num_missing > 0:
        op.error("missing %d required argument(s)" % num_missing)

    ports = []
    for pair in args[3].split(","):
        if ":" not in pair:
            op.error("incorrect port syntax: %s" % pair)
        ports.append([int(port) for port in pair.split(":", 1)])

    return options, args, ports


def run_diagnostic(domains, ports, local_host):
    errors = []

    # Checking domains to forward
    domain_pat = re.compile("^([\\da-z\\.-]+)\\.([a-z\\.]{2,8})$")
    for dom in domains:
        if not domain_pat.search(dom):
            errors.append("Incorrect domain given: %s" % dom)

    # Checking if host is accessible
    for pair in ports:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.connect((local_host, pair[0]))
        except socket.gaierror:
            errors.append("Local host %s is not accessible" % local_host)
            break
        except socket.error, (_, port_error):
            errors.append("Problem connecting to %s:%s: %s"
                          % (local_host, pair[0], port_error))

    if errors == []:
        print "No errors found, proceeding"
        return
    else:
        print "Errors found:"
        for err in errors:
            print "\t%s" % err
        sys.exit(1)


def connect_tunnel(options, tunnel, tunnel_change_callback):
    def daemonize():
        daemon.daemonize(options.pidfile)
        if drop_readyfile: drop_readyfile()

    drop_readyfile = None
    if options.readyfile:
        drop_readyfile = lambda : open(options.readyfile, 'wb').write("ready")

    if options.daemonize:
        tunnel_change_callback(tunnel, connected_callback=daemonize)
    elif drop_readyfile:
        tunnel_change_callback(tunnel, connected_callback=drop_readyfile)
    else:
        tunnel_change_callback(tunnel)


def main(options, args, ports):
    username = args[0]
    access_key = args[1]
    local_host = args[2]
    domains = ",".join(args[4:]).split(",")

    if options.diagnostic:
        run_diagnostic(domains, ports, local_host)

    sauce_client = saucerest.SauceClient(name=username, access_key=access_key,
                                         base_url=options.base_url)
    if sauce_client.get_tunnel("test-authorized")['error'] == 'Unauthorized':
        print "Error: User/access-key combination is incorrect"
        sys.exit(1)

    if options.shutdown:
        print "Searching for existing tunnels using requested domains..."
        for tunnel in sauce_client.list_tunnels():
            for domain in (d for d in domains if d in tunnel['DomainNames']):
                print "tunnel %s is currenty using requested domain %s" % (
                      tunnel['_id'], domain)
                print "shutting down tunnel %s" % tunnel['_id']
                sauce_client.delete_tunnel(tunnel['_id'])


    def tunnel_change_callback(new_tunnel, connected_callback=None):
        global tunnel_id
        tunnel_id = new_tunnel['id']
        sshtunnel.connect_tunnel(
            tunnel_id, sauce_client.base_url, username, access_key, local_host,
            new_tunnel['Host'], ports, connected_callback,
            lambda: sauce_client.delete_tunnel(tunnel_id), options.diagnostic)

    try:
        tunnel = get_new_tunnel(sauce_client, domains)
        connect_tunnel(options, tunnel, tunnel_change_callback)
        heartbeat(username, access_key, sauce_client.base_url,
                  tunnel_id, tunnel_change_callback)
        reactor.run()
    finally:
        print "Aborted -- shutting down tunnel machine"
        sauce_client.delete_tunnel(tunnel_id)


if __name__ == '__main__':
    options, args, ports = _parse_options()
    main(options, args, ports)
