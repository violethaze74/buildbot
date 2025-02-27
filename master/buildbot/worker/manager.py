# This file is part of Buildbot.  Buildbot is free software: you can
# redistribute it and/or modify it under the terms of the GNU General Public
# License as published by the Free Software Foundation, version 2.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
# details.
#
# You should have received a copy of the GNU General Public License along with
# this program; if not, write to the Free Software Foundation, Inc., 51
# Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#
# Copyright Buildbot Team Members


from twisted.internet import defer
from twisted.python import log

from buildbot.process.measured_service import MeasuredBuildbotServiceManager
from buildbot.util import misc
from buildbot.worker.protocols import pb as bbpb


class WorkerRegistration:

    __slots__ = ['master', 'worker', 'pbReg']

    def __init__(self, master, worker):
        self.master = master
        self.worker = worker

    def __repr__(self):
        return "<{} for {}>".format(self.__class__.__name__, repr(self.worker.workername))

    @defer.inlineCallbacks
    def unregister(self):
        bs = self.worker
        # update with portStr=None to remove any registration in place
        yield self.master.workers.pb.updateRegistration(
            bs.workername, bs.password, None)
        yield self.master.workers._unregister(self)

    @defer.inlineCallbacks
    def update(self, worker_config, global_config):
        # For most protocols, there's nothing to do, but for PB we must
        # update the registration in case the port or password has changed.
        if 'pb' in global_config.protocols:
            self.pbReg = yield self.master.workers.pb.updateRegistration(
                worker_config.workername, worker_config.password,
                global_config.protocols['pb']['port'])

    def getPBPort(self):
        return self.pbReg.getPort()


class WorkerManager(MeasuredBuildbotServiceManager):

    name = "WorkerManager"
    managed_services_name = "workers"

    config_attr = "workers"
    PING_TIMEOUT = 10
    reconfig_priority = 127

    def __init__(self, master):
        super().__init__()

        self.pb = bbpb.Listener(master)

        # WorkerRegistration instances keyed by worker name
        self.registrations = {}

        # connection objects keyed by worker name
        self.connections = {}

    @property
    def workers(self):
        # self.workers contains a ready Worker instance for each
        # potential worker, i.e. all the ones listed in the config file.
        # If the worker is connected, self.workers[workername].worker will
        # contain a RemoteReference to their Bot instance. If it is not
        # connected, that attribute will hold None.
        # workers attribute is actually just an alias to multiService's
        # namedService
        return self.namedServices

    def getWorkerByName(self, workerName):
        return self.registrations[workerName].worker

    def register(self, worker):
        # TODO: doc that reg.update must be called, too
        workerName = worker.workername
        reg = WorkerRegistration(self.master, worker)
        self.registrations[workerName] = reg
        return defer.succeed(reg)

    def _unregister(self, registration):
        del self.registrations[registration.worker.workername]

    @defer.inlineCallbacks
    def newConnection(self, conn, workerName):
        if workerName in self.connections:
            log.msg(("Got duplication connection from '{}'"
                     " starting arbitration procedure").format(workerName))
            old_conn = self.connections[workerName]
            try:
                yield misc.cancelAfter(self.PING_TIMEOUT,
                                       old_conn.remotePrint("master got a duplicate connection"),
                                       self.master.reactor)
                # if we get here then old connection is still alive, and new
                # should be rejected
                raise RuntimeError("rejecting duplicate worker")
            except defer.CancelledError:
                old_conn.loseConnection()
                log.msg("Connected worker '{}' ping timed out after {} seconds".format(workerName,
                        self.PING_TIMEOUT))
            except RuntimeError:
                raise
            except Exception as e:
                old_conn.loseConnection()
                log.msg("Got error while trying to ping connected worker {}:{}".format(workerName,
                                                                                       e))
            log.msg("Old connection for '{}' was lost, accepting new".format(workerName))

        try:
            yield conn.remotePrint(message="attached")
            info = yield conn.remoteGetWorkerInfo()
            log.msg("Got workerinfo from '{}'".format(workerName))
        except Exception as e:
            log.msg("Failed to communicate with worker '{}'\n{}".format(workerName, e))
            raise

        conn.info = info
        self.connections[workerName] = conn

        def remove():
            del self.connections[workerName]
        conn.notifyOnDisconnect(remove)

        # accept the connection
        return True
