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
import threading

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


def get_new_tunnel(sauce_client, domains, replace=True, max_tries=1000):
    tunnel = None
    tries = 0
    while not tunnel:
        tries += 1
        trymsg = ("(try #%d)" % tries) if tries > 1 else ""

        if replace:
            sauce_client.delete_tunnels_by_domains(domains)

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


def exc_to_const(f, exc=Exception, const=False):
    def inner(*a, **k):
        try:
            return f(*a, **k)
        except exc:
            return const

    inner.__name__ = f.__name__
    inner.__doc__ = f.__doc__
    return inner


class Heartbeat(threading.Thread):
    def __init__(self, sauce_client, tunnel_id, update_callback,
                 max_tries=1000):
        threading.Thread.__init__(self)
        self.sauce_client = sauce_client
        self.tunnel_id = tunnel_id
        self.update_callback = update_callback
        self.max_tries = max_tries
        self.done = False
        self.interval = RETRY_TIME

    def run(self):
        while not self.done:
            try:
                self.heartbeat()
            except Exception, e:
                logger.error("Error in heartbeat: %s %s", type(e), str(e))
            time.sleep(self.interval)

    def heartbeat(self):
        is_tunnel_healthy = exc_to_const(self.sauce_client.is_tunnel_healthy)
        self.sauce_client.prune_unhealthy_tunnels([self.tunnel_id])
        if not is_tunnel_healthy(self.tunnel_id):
            running = False
            tries = 0
            while not self.done:
                tries += 1
                try:
                    tunnel = self.sauce_client.get_tunnel(self.tunnel_id)

                    if 'UserShutDown' in tunnel:
                        _do_user_shutdown(self.sauce_client, self.tunnel_id)
                        return

                    if tunnel['Status'] == "running":
                        running = True
                        break

                    logger.info("Tunnel is down")
                    self.sauce_client.delete_tunnel(self.tunnel_id)
                except saucerest.SauceRestError, e:
                    logger.critical(
                        "Unable to connect to REST interface at %s: %s",
                        self.sauce_client.base_url, e)

                    if self.max_tries and tries >= self.max_tries:
                        logger.critical("Exceeded max retries, giving up")
                        if reactor.running:
                            reactor.stop()
                        else:
                            sys.exit(1)

                    time.sleep(RETRY_TIME)
                else:
                    break

            if self.done:
                return

            logger.info("Replacing tunnel")
            new_tunnel = get_new_tunnel(self.sauce_client,
                                        tunnel['DomainNames'])
            self.tunnel_id = new_tunnel['id']

            if self.update_callback:
                new_tunnel = self.sauce_client.get_tunnel(self.tunnel_id)
                reactor.callFromThread(self.update_callback, new_tunnel)
