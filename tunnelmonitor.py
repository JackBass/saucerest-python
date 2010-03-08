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
import time
import logging

from twisted.internet import reactor

import saucerest

TIMEOUT = 600
RETRY_TIME = 5
logger = logging.getLogger(__name__)


def _get_running_tunnel(sauce_client, tunnel_id):
    """
    Wait up to TIMEOUT seconds for tunnel to have "running" status. Return
    running tunnel or None if timeout is reached.
    """
    last_status = None
    for _ in xrange(TIMEOUT / RETRY_TIME):
        tunnel = sauce_client.get_tunnel(tunnel_id)

        if tunnel['Status'] != last_status:
            print "Status: %s" % tunnel['Status']
            last_status = tunnel['Status']

        if tunnel['Status'] == "running":
            return tunnel
        elif tunnel['Status'] == 'terminated':
            logger.warning("Tunnel is terminated")
            sauce_client.delete_tunnel(tunnel['id'])
            return None
        time.sleep(RETRY_TIME)

    logger.warning("Timed out after waiting ~%ds for running tunnel" % TIMEOUT)
    return None


def get_new_tunnel(sauce_client, domains):
    max_tries = 5
    tunnel = None
    tried = 0
    while not tunnel:
        print "Launching tunnel ... (try #%d)" % (tried + 1)
        tunnel = sauce_client.create_tunnel({'DomainNames': domains})
        tried += 1
        if 'error' in tunnel:
            print "Error: %s" % tunnel['error']
            if tried >= max_tries:
                print "Could not launch tunnel (tried %d times)"
                sys.exit(1)
            time.sleep(RETRY_TIME)
            tunnel = None
        else:
            tunnel = _get_running_tunnel(sauce_client, tunnel['id'])

    print "Tunnel ID: %s" % tunnel['id']
    return tunnel


def heartbeat(name, key, base_url, tunnel_id, update_callback):
    sauce_client = saucerest.SauceClient(name, key, base_url)
    if sauce_client.is_tunnel_healthy(tunnel_id):
        reactor.callLater(RETRY_TIME, heartbeat, name, key, base_url,
                          tunnel_id, update_callback)
    else:
        tunnel_settings = sauce_client.get_tunnel(tunnel_id)
        if 'UserShutDown' in tunnel_settings:
            print "Tunnel shutting down on user request"
            return
        print "Tunnel is down"
        sauce_client.delete_tunnel(tunnel_id)

        print "Replacing down tunnel"
        new_tunnel = get_new_tunnel(
            sauce_client, tunnel_settings['DomainNames'])
        if update_callback:
            new_tunnel = sauce_client.get_tunnel(new_tunnel['id'])
            update_callback(new_tunnel)
