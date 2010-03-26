#! /usr/bin/python
# -*- coding: utf-8 -*-
#
# Copyright (c) 2009-2010 Sauce Labs Inc
# Copyright (c) 2001-2004 Twisted Matrix Laboratories.
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

import logging

from twisted.internet import defer, protocol, reactor, task
from twisted.conch.error import ConchError
from twisted.conch.ssh import (
    connection, channel, userauth, transport, forwarding)

logger = logging.getLogger(__name__)


class TunnelTransport(transport.SSHClientTransport):

    def __init__(self,
                 user,
                 password,
                 forward_host,
                 forward_port,
                 forward_remote_port,
                 connected_callback=None,
                 error_callback=None,
                 diagnostic=False):
        try:
            transport.SSHClientTransport.__init__(self)
        except AttributeError:
            pass
        self.user = user
        self.password = password
        self.forward_host = forward_host
        self.forward_port = forward_port
        self.forward_remote_port = forward_remote_port
        self.connected_callback = connected_callback
        self.error_callback = error_callback
        self.diagnostic = diagnostic

    def verifyHostKey(self, hostKey, fingerprint):
        return defer.succeed(1)

    def connectionSecure(self):
        self.requestService(
            TunnelUserAuth(self.user,
                           TunnelConnection(self.forward_host,
                           self.forward_port,
                           self.forward_remote_port,
                           self.connected_callback,
                           self.error_callback,
                           self.diagnostic),
                           self.password))

    def receiveError(self, reasonCode, description):
        logger.warning('Got remote error, code %s, reason: %s',
                       reasonCode, description)
        if self.error_callback:
            self.error_callback()

    def connectionLost(self, reason):
        logger.warning('SSH connection lost, reason: %s',
                       reason)
        if self.error_callback:
            self.error_callback()


class TunnelUserAuth(userauth.SSHUserAuthClient):

    def __init__(self, user, connection, password):
        userauth.SSHUserAuthClient.__init__(self, user, connection)
        self.password = password

    def getPassword(self):
        return defer.succeed(self.password)

    def getGenericAnswers(self, name, instruction, questions):
        print "name:", name
        print "instruction:", instruction
        print "questions:", questions
        answers = []
        for prompt, echo in questions:
            answer = self.password
            answers.append(answer)
        return defer.succeed(answers)

    def getPublicKey(self):
        return

    def getPrivateKey(self):
        return


class _KeepAlive:

    def __init__(self, conn):
        self.conn = conn
        self.globalTimeout = None
        self.lc = task.LoopingCall(self.sendGlobal)
        self.lc.start(300)

    def sendGlobal(self):
        d = self.conn.sendGlobalRequest("tunnel-keep-alive@saucelabs.com",
                                        "",
                                        wantReply = 1)
        d.addBoth(self._cbGlobal)
        self.globalTimeout = reactor.callLater(30, self._ebGlobal)

    def _cbGlobal(self, res):
        if self.globalTimeout:
            self.globalTimeout.cancel()
            self.globalTimeout = None

    def _ebGlobal(self):
        if self.globalTimeout:
            self.globalTimeout = None
            self.conn.transport.loseConnection()


class TunnelConnection(connection.SSHConnection):

    def __init__(self,
                 forward_host,
                 forward_port,
                 forward_remote_port,
                 connected_callback=None,
                 error_callback=None,
                 diagnostic=False):
        try:
            connection.SSHConnection.__init__(self)
        except AttributeError:
            pass

        self.forward_host = forward_host
        self.forward_port = forward_port
        self.forward_remote_port = forward_remote_port
        self.connected_callback = connected_callback
        self.error_callback = error_callback
        self.diagnostic = diagnostic

    def serviceStarted(self):
        self.remoteForwards = {}
        if hasattr(self.transport, 'sendIgnore'):
            _KeepAlive(self)
        self.requestRemoteForwarding(self.forward_remote_port,
                                     (self.forward_host, self.forward_port))
        self.openChannel(NullChannel())

    def requestRemoteForwarding(self, remotePort, hostport):
        data = forwarding.packGlobal_tcpip_forward(('0.0.0.0', remotePort))
        d = self.sendGlobalRequest('tcpip-forward',
                                   data,
                                   wantReply=1)
        logger.info("requesting remote forwarding %s=>%s:%s"
                    % (remotePort, hostport[0], hostport[1]))
        d.addCallback(self._cbRemoteForwarding, remotePort, hostport)
        d.addErrback(self._ebRemoteForwarding, remotePort, hostport)

    def _cbRemoteForwarding(self, result, remotePort, hostport):
        logger.info("accepted remote forwarding %s=>%s:%s"
                    % (remotePort, hostport[0],hostport[1]))
        self.remoteForwards[remotePort] = hostport
        if self.connected_callback:
            self.connected_callback()

    def _ebRemoteForwarding(self, f, remotePort, hostport):
        logger.error("remote forwarding %s=>%s:%s failed"
                     % (remotePort, hostport[0], hostport[1]))
        logger.error(str(f))

    def cancelRemoteForwarding(self, remotePort):
        data = forwarding.packGlobal_tcpip_forward(('0.0.0.0', remotePort))
        self.sendGlobalRequest('cancel-tcpip-forward', data)
        logger.warning("cancelling remote forwarding %s" % remotePort)
        try:
            del self.remoteForwards[remotePort]
        except:
            pass

    def channel_forwarded_tcpip(self, winSize, maxP, data):
        if self.diagnostic:
            logger.debug("FTCP %s" % repr(data))
        remoteHP, origHP = forwarding.unpackOpen_forwarded_tcpip(data)
        if self.diagnostic:
            logger.debug(str(remoteHP))
        if remoteHP[1] in self.remoteForwards:
            connectHP = self.remoteForwards[remoteHP[1]]
            if self.diagnostic:
                logger.debug("connect forwarding %s" % str(connectHP))
            return forwarding.SSHConnectForwardingChannel(connectHP,
                                                          remoteWindow=winSize,
                                                          remoteMaxPacket=maxP,
                                                          conn = self)
        else:
            raise ConchError(connection.OPEN_CONNECT_FAILED,
                             "don't know about that port")

    def channelClosed(self, channel):
        if self.diagnostic:
            logger.debug("connection closing %s" % channel)
            logger.debug(str(self.channels))
        if len(self.channels) == 1: # just us left
            logger.warning("stopping connection to a closed tunnel")
            if self.error_callback:
                self.error_callback()
            try:
                #dont stop reactor when one connection is closed
                self.__class__.__bases__[0].channelClosed(self, channel)
            except:
                pass
        else:
            # because of the unix thing
            self.__class__.__bases__[0].channelClosed(self, channel)


class NullChannel(channel.SSHChannel):

    name = 'session'

    def openFailed(self, reason):
        logger.error("NullChannel open failed: %s" % reason)

    def channelOpen(self, ignoredData):
        return

    def closeReceived(self):
        logger.info("remote side closed %s" % self)
        self.conn.sendClose(self)

    def closed(self):
        global old
        logger.info("closing channel")

"""
Tunnel handling:
"""

open_tunnels = 0

def connect_tunnel(tunnel_id,
                 base_url,
                 username,
                 access_key,
                 local_host,
                 remote_host,
                 ports,
                 connected_callback,
                 error_callback,
                 shutdown_callback,
                 diagnostic):

    def check_n_call():
        global open_tunnels
        open_tunnels += 1
        if open_tunnels >= len(ports) and connected_callback:
            connected_callback()

    def eb(failure):
        logger.warning(str(failure))
        if error_callback:
            error_callback()
    for (local_port, remote_port) in ports:
        df = protocol.ClientCreator(reactor,
                                    TunnelTransport,
                                    username,
                                    access_key,
                                    local_host,
                                    local_port,
                                    remote_port,
                                    check_n_call,
                                    error_callback,
                                    diagnostic).connectTCP(remote_host, 22)
        df.addErrback(eb)

    reactor.addSystemEventTrigger("before", "shutdown", shutdown_callback)

