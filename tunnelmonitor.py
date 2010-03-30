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


def _do_user_shutdown(sauce_client, tunnel_id):
    logger.info("Tunnel shutting down on user request")
    sauce_client.delete_tunnel(tunnel_id)
    if reactor.running:
        reactor.stop()
    else:
        sys.exit(0)


def _get_running_tunnel(sauce_client, tunnel_id):
    """
    Wait up to TIMEOUT seconds for tunnel to have "running" status. Return
    running tunnel or None if timeout is reached.
    """
    last_status = None
    for _ in xrange(TIMEOUT / RETRY_TIME):
        tunnel = sauce_client.get_tunnel(tunnel_id)
        assert tunnel['id'] == tunnel_id, \
            "Tunnel info should have same ID as the one requested"

        if tunnel['Status'] != last_status:
            logger.info("Status: %s", tunnel['Status'])
            last_status = tunnel['Status']

        if tunnel['Status'] == "running":
            return tunnel
        elif tunnel['Status'] == 'terminated':
            if 'UserShutDown' in tunnel:
                _do_user_shutdown(sauce_client, tunnel['id'])
            logger.warning("Tunnel is terminated")
            sauce_client.delete_tunnel(tunnel['id'])
            return None
        time.sleep(RETRY_TIME)

    logger.warning("Timed out after waiting ~%ds for running tunnel", TIMEOUT)
    return None


def get_new_tunnel(sauce_client, domains, max_tries=None):
    tunnel = None
    tries = 0
    while not tunnel:
        tries += 1
        trymsg = ("(try #%d)" % tries) if tries > 1 else ""
        logger.info("Launching tunnel ... %s", trymsg)
        try:
            tunnel = sauce_client.create_tunnel({'DomainNames': domains})
        except saucerest.SauceRestError, e:
            tunnel = dict(
                error="Unable to connect to REST interface: %s" % str(e))
        if 'error' in tunnel:
            logger.warning("Tunnel error: %s", tunnel['error'])
            if max_tries and tries >= max_tries:
                logger.error("Exiting: Could not launch tunnel"
                             " (tries %d times)", tries)
                if reactor.running:
                    reactor.stop()
                else:
                    sys.exit(1)
            time.sleep(RETRY_TIME)
            tunnel = None
        else:
            try:
                tunnel = _get_running_tunnel(sauce_client, tunnel['id'])
            except saucerest.SauceRestError:
                logger.error("Created tunnel, but could not retrieve info")
                tunnel = None

    logger.info("Tunnel host: %s", tunnel['Host'])
    logger.info("Tunnel ID: %s", tunnel['id'])
    return tunnel


def heartbeat(sauce_client, tunnel_id, update_callback, max_tries=None):
    if sauce_client.is_tunnel_healthy(tunnel_id):
        reactor.callLater(RETRY_TIME, heartbeat, sauce_client,
                          tunnel_id, update_callback)
    else:
        tries = 0
        while True:
            tries += 1
            try:
                tunnel = sauce_client.get_tunnel(tunnel_id)

                if 'UserShutDown' in tunnel:
                    _do_user_shutdown(sauce_client, tunnel_id)
                    return

                logger.info("Tunnel is down")
                sauce_client.delete_tunnel(tunnel_id)
            except saucerest.SauceRestError, e:
                logger.critical(
                    "Unable to connect to REST interface at %s: %s",
                    sauce_client.base_url, e)

                if max_tries and tries >= max_tries:
                    logger.critical("Exceeded max retries, giving up")
                    if reactor.running:
                        reactor.stop()
                    else:
                        sys.exit(1)

                time.sleep(RETRY_TIME)
            else:
                break

        logger.info("Replacing tunnel")
        new_tunnel = get_new_tunnel(sauce_client, tunnel['DomainNames'])

        if update_callback:
            new_tunnel = sauce_client.get_tunnel(new_tunnel['id'])
            update_callback(new_tunnel)
