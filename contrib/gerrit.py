#!/usr/bin/python
#
# TODO:
# - Figure out a way to catch up on changes that might be missed while
#   the buildbot is down.
# - Populate GerritChange.files by connecting back to Gerrit's web-interface
#   to get this information
# - GerritStatusPush only updates Gerrit once at the end of the BuildSet.
#   Consider updating Gerrit incrementally as each builder finishes. We
#   could add a buildFinished method and push the results to Gerrit as each
#   builder finishes, then use buildsetFinished to set the verified flag. We'd
#   want to be clever and detect the last buildFinished so we don't need two
#   ssh connections at the very end. Alternately, we might want to have a
#   separate gerrit account for each builder.
# - If multiple dependent changes are uploaded, each gets built individually.
#   Consider instead building only the tip-most change... but then we can't
#   accurately set the verified status.

"""
Integrate with Gerrit Code Review. I contain three useful classes:

1. GerritScheduler - use this class to automatically initiate a build whenever
   a new change is created in Gerrit.

2. GerritChangeSource - use this class in concert with GerritScheduler.
   GerritScheduler will create a default instance which is typically
   sufficient, but you may create your own instance to apply a category to the
   changes created by this class.

3. GerritStatusPush - use this class to automatically update the Gerrit
   change with the results of a build.

GerritChangeSource and GerritStatusPush require that the buildbot have ssh
access to gerrit. Typically you would setup a "service" account within gerrit,
then setup .ssh/config with the information that the buildbot needs to connect
to gerrit. e.g.:

Host gerrit
    HostName gerrit.example.com
    Port 29418
    User buildbot

Of course, you will have previously created an ssh-key pair and registered the
public-key with Gerrit, and further confirmed that the buildbot user can
ssh to gerrit. e.g. "ssh -p 29418 buildbot@gerrit.example.com"
"""

import os
import random
import sys

try:
    import simplejson as json
except ImportError:
    import json

from buildbot import buildset, util
from buildbot.changes import base
from buildbot.changes.changes import Change
from buildbot.scheduler import TryBase
from buildbot.sourcestamp import SourceStamp
from buildbot.status.builder import SUCCESS, WARNINGS, FAILURE, EXCEPTION
from buildbot.status.persistent_queue import PersistentQueue
from buildbot.status.status_push import StatusPush
from twisted.internet import protocol, reactor
from twisted.internet.utils import  getProcessOutputAndValue

from twisted.python import log

class _EventFactory(dict):
    def __getattr__(self, name):
        return self[name]

class GerritChange(Change):
    pass

def _GerritChangeFactory(event, category):
    if event.type != 'patchset-created':
        return
    c = event.change
    ps = event.patchSet
    c_dir = ("%02d" % int(c.number))[-2:]
    # see http://groups.google.com/group/repo-discuss/msg/b324266994032261
    c_ref = "refs/changes/%s/%s/%s" % (c_dir, c.number, ps.number)
    properties = {
        "branch":c.branch, # the change's destination branch
        "change_id":c.id,
        "change_number":c.number,
        "patchset_number":ps.number,
        "project":c.project,
    }
    return GerritChange(
        branch=c_ref,
        category=category,
        comments=("%s\nPatch Set %s\n" % (c.subject, ps.number)),
        files=[],
        properties=properties,
        revision=ps.revision,
        revlink=c.url,
        who=("%s <%s>" % (c.owner.name, c.owner.email)),
    )

class _ExponentialBackoffMixin:
    # Exponential backoff code re-purposed from ReconnectingClientFactory
    delayMax = 60
    delayCurrent = delayInitial = 1.0
    delayFactor = 1.6180339887498948
    delayJitter = 0.11962656492
    delayRetries = 0
    
    def resetDelay(self):
        self.delayCurrent = self.delayInitial
        self.delayRetries = 0
    
    def increaseDelay(self):
        self.delayRetries += 1
        d = min(self.delayCurrent * self.delayFactor, self.delayMax)
        self.delayCurrent = random.normalvariate(d, d * self.delayJitter)
        return self.delayCurrent

class _GerritPP(protocol.ProcessProtocol, _ExponentialBackoffMixin):
    connected = False
    terminated = False
    
    def __init__(self, host, sshbin, callback):
        self.host = host
        self.sshbin = sshbin
        self.callback = callback
    
    def connect(self):
        argv = (self.sshbin, '-2', '-v', '-T', '-n', '-a',
            '-o', 'BatchMode=yes',
            '-o', 'ClearAllForwardings=yes',
            '-o', 'ConnectTimeout=30',
            '-o', 'ServerAliveInterval=10',
            '-o', 'ServerAliveCountMax=3',
            self.host, 'gerrit', 'stream-events')
        reactor.spawnProcess(self, argv[0], argv, os.environ)
    
    def terminate(self):
        self.terminated = True
        self.transport.signalProcess('KILL')
    
    def connectionMade(self):
        self.connected = True
        self.transport.closeStdin()
        log.msg("GerritChangeSource(%s) started ssh" % self.host)
        # all we know is that we spawned the process, not that we're
        # connected. we won't know that for a bit...
    
    def outReceived(self, data):
        for line in data.splitlines():
            try:
                event = json.loads(line, object_hook=_EventFactory)
            except json.decoder.JSONDecodeError:
                log.msg("GerritChangeSource(%s) invalid JSON: %r" %
                        (self.host, line))
                log.err()
            else:
                self.callback(event)
    
    def errReceived(self, data):
        # Parsing the debug output of ssh is probably brittle, but how else to
        # determine when stream-events has actually been started?
        for line in data.splitlines():
            if line == 'debug1: Sending command: gerrit stream-events':
                log.msg("GerritChangeSource(%s) started gerrit stream-events" %
                        self.host)
                self.resetDelay()
            if line.startswith('debug1: '):
                continue
            log.msg("GerritChangeSource(%s) ssh stderr: %s" % (self.host, line))
    
    def processEnded(self, status):
        self.connected = False
        if self.terminated:
            return
        log.msg("GerritChangeSource(%s) ssh ended; %s" %
                (self.host, status.value))
        delay = self.increaseDelay()
        log.msg("GerritChangeSource(%s) restarting ssh in %d seconds" %
                (self.host, delay))
        reactor.callLater(delay, self.connect)

class GerritChangeSource(base.ChangeSource, util.ComparableMixin):
    """GerritChangeSource opens an ssh connection to a Gerrit server and watches
    the output of stream-events for changes, submitting new changes to its
    change master, which is typically a GerritScheduler instance.
    """
    
    compare_attrs = ['host', 'sshbin']
    connected = False
    
    def __init__(self, host='gerrit', sshbin='ssh', category=None):
        """
        @param host: the hostname of the Gerrit server - default is 'gerrit'
        @param sshbin: /path/to/ssh - default is 'ssh'
        @param category: a category to apply to all changes - no default.
        """
        self.pp = _GerritPP(host, sshbin, self._handle_event)
        self.category = category
    
    def startService(self):
        log.msg("GerritChangeSource(%s) starting" % self.pp.host)
        base.ChangeSource.startService(self)
        reactor.callLater(0, self.pp.connect)
    
    def stopService(self):
        log.msg("GerritChangeSource(%s) shutting down" % self.pp.host)
        self.pp.terminate()
        return base.ChangeSource.stopService(self)
    
    def describe(self):
        online = ""
        if not self.pp.connected:
            online = " [OFFLINE]"
        return "GerritChangeSource(%s)%s" % (self.pp.host, online)
    
    def _handle_event(self, event):
        change = _GerritChangeFactory(event, self.category)
        if change is None:
            return
        log.msg("GerritChangeSource(%s) new change: %s" %
                (self.pp.host, change.asDict()))
        self.parent.addChange(change)

class GerritSourceStamp(SourceStamp):
    pass

class GerritScheduler(TryBase):
    """GerritScheduler will run a build whenever a new change is uploaded to
    Gerrit. Restrict which changes cause a build using the branches, projects,
    and categories parameters. To set a category for changes, instantiate your
    own GerritChangeSource and pass that in as the source parameter.
    """
    
    def __init__(self, name, builderNames, source=None, properties=None,
                 branches=None, projects=None, categories=None):
        """
        @param name: the name of this GerritScheduler
        @param builderNames: a list of Builder names. When this GerritScheduler
                             decides to start a set of builds, they will be
                             run on the Builders named by this list.
        
        @param source: a GerritChangeSource instance. This is not normally
                       needed unless you want to override the default host
                       ('gerrit') or sshbin ('ssh') params of GerritChangeSource
        
        @param properties: properties to apply to all builds started from this
                           scheduler
        
        @param branches: a list of branch names. Only changes destined for these
                         branches will trigger builds. By default all changes
                         trigger builds.
        
        @param projects: a list of project names. Only changes uploaded to these
                          projects will trigger builds. By default all changes
                          trigger builds.
        
        @param categories: A list of categories of changes to accept. Changes
                           will not have a category unless you pass in a
                           GerritChangeSource which sets the category.
        """
        if properties is None:
            properties = {}
        TryBase.__init__(self, name, builderNames, properties)
        if source is None:
            source = GerritChangeSource()
        assert isinstance(source, GerritChangeSource)
        self.watcher = source
        self.watcher.setServiceParent(self)
        self.branches = branches
        self.projects = projects
        self.categories = categories
    
    def addChange(self, c):
        if not isinstance(c, GerritChange):
            return
        p = c.properties
        if self.branches is not None and p['branch'] not in self.branches:
            return
        if self.projects is not None and p['project'] not in self.projects:
            return
        if self.categories is not None and c.category not in self.categories:
            return
        short_id = p['change_id'][:9]
        ps_number = p['patchset_number']
        bsid = "Change %s Patch Set %s" % (short_id, ps_number)
        reason = "%s created by %s" % (bsid, c.who)
        ss = GerritSourceStamp(changes=[c])
        bs = buildset.BuildSet(self.builderNames, ss, reason=reason,
                               bsid=bsid, properties=self.properties)
        self.submitBuildSet(bs)

def _shell_escape(x):
    """Escape argument according to shell-quoting rules"""
    # Sadly, ssh collapses distinct arguments into a single string before
    # sending to the remote end, so we have to escape them; identical to
    # commands.mkarg (which is deprecated in Python 3.0)
    if '\'' not in x:
        return ' \'' + x + '\''
    s = ' "'
    for c in x:
        if c in '\\$"`':
            s = s + '\\'
        s = s + c
    s = s + '"'
    return s

class GerritStatusPush(StatusPush, _ExponentialBackoffMixin):
    """GerritStatusPush updates Gerrit with the results of a build. It waits
    for all builds in the buildset to complete then uses "gerrit approve" to
    update the change. If the buildset as a whole is succcesful then the
    change is is set to "+1" verified, otherwise it is set to "-1" verified.
    """
    
    what_dict = {
        SUCCESS  : "succeeded",
        FAILURE  : "failed",
        WARNINGS : "completed with warnings",
        EXCEPTION: "encountered an exception",
    }
    what_unknown = "completed with an unknown result."
    verify_dict = {
        SUCCESS: "1",
        FAILURE: "0",
    }
    lastPushWasSuccessful = True
    
    def __init__(self, host='gerrit', sshbin='ssh'):
        """
        @param host: the hostname of the Gerrit server - default is 'gerrit'
        @param sshbin: /path/to/ssh - default is 'ssh'
        """
        self.host = host
        self.sshbin = sshbin
        queue = PersistentQueue(path='gerritstatus-%s' % host)
        StatusPush.__init__(self, GerritStatusPush.pushGerrit, queue=queue)
    
    def push(self, event, **objs):
        pass
    
    def wasLastPushSuccessful(self):
        return self.lastPushWasSuccessful
    
    def buildsetSubmitted(self, bss):
        # Only subscribe to buildsets we care about
        if not isinstance(bss.getSourceStamp(), GerritSourceStamp):
            return
        bss.waitUntilFinished().addCallback(self.buildsetFinished)
    
    def buildsetFinished(self, bss):
        # Aggregate the results and queue them for pushing to Gerrit
        msgs = []
        get_name = lambda bs:bs.getBuilder().getName()
        for brs in bss.getBuildRequests(): # BuildRequestStatus
            for bs in sorted(brs.getBuilds(), key=get_name): # BuildStatus
                name = get_name(bs)
                what = self.what_dict.get(bs.getResults(), self.what_unknown)
                text = ' '.join(bs.getText())
                link = self.status.getURLForThing(bs)
                if link:
                    link = " - " + link
                else:
                    link = '.'
                msgs.append("Builder %s %s (%s)%s" % (name, what, text, link))
        msg = '\n\n'.join(msgs)
        val = self.verify_dict.get(bss.getResults(), "0")
        rev = bss.getSourceStamp().revision
        self.queue.pushItem((rev, val, msg))
        if self.task is None or not self.task.active():
            self.queueNextServerPush()
    
    def pushGerrit(self):
        items = self.queue.popChunk(1)
        (rev, val, msg) = items[0]
        
        def Success(result):
            (out, err, code) = result
            if code != 0:
                return Failure((out, err, None), code)
            log.msg('GerritStatusPush(%s) gerrit approve %s %s' %
                    (self.host, rev, (out, err)))
            self.lastPushWasSuccessful = True
            self.resetDelay()
            self.queueNextServerPush()
        
        def Failure(result, code=None):
            (out, err, signal) = result
            log.msg('GerritStatusPush(%s) gerrit approve %s failed %s' %
                    (self.host, rev,
                     ("exit code %s" % code, "signal %s" % signal, out, err)))
            self.queue.insertBackChunk(items)
            if self.stopped:
                self.queue.save()
            self.lastPushWasSuccessful = False
            self.retryDelay = self.increaseDelay()
            self.queueNextServerPush()
        
        argv = (self.sshbin, '-2', '-T', '-n', '-a',
            '-o', 'BatchMode=yes',
            '-o', 'ClearAllForwardings=yes',
            '-o', 'ConnectTimeout=30',
            self.host, 'gerrit', 'approve', rev.encode('ascii'),
            '--verified', val, '-m', _shell_escape(msg))
        
        log.msg("GerritStatusPush: %s" % repr(argv))
        deferred = getProcessOutputAndValue(argv[0], argv[1:], os.environ)
        deferred.addCallbacks(Success, Failure)
        return deferred

#############
# Testing

_mock_event = {
 'change': {'branch': 'master',
            'id': 'Ideadbeefdeadbeefdeadbeefdeadbeefdeadbeef',
            'number': '23',
            'owner': {'email': 'user@domain.com', 'name': 'Anonymous Coward'},
            'project': 'test project',
            'subject': 'test subject',
            'url': 'http://gerrit/1'},
 'patchSet': {'number': '1',
              'revision': 'deadbeefdeadbeefdeadbeefdeadbeefdeadbeef'},
 'type': 'patchset-created'
}

class _MockScheduler:
    def submitBuildSet(self, bs):
        print "got %r" % bs

def main():
    event = json.loads(json.dumps(_mock_event), object_hook=_EventFactory)
    print _GerritChangeFactory(event, '').asText()
    log.startLogging(sys.stdout)
    s = GerritScheduler("test", ["testBuilder"])
    s.parent = _MockScheduler()
    s.startService()
    reactor.run()

if __name__ == '__main__':
    main()
