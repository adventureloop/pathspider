
import sys
import collections
import logging
import signal
import os

from subprocess import Popen
from subprocess import PIPE
from subprocess import TimeoutExpired

from pathspider.base import DesynchronizedSpider
from pathspider.base import PluggableSpider
from pathspider.base import NO_FLOW

from pathspider.observer.dummy import Observer

SpiderRecord = collections.namedtuple("SpiderRecord", ["ip", "rport", "port",
                                                       "host", "rank",
                                                       "config",
                                                       "connstate", "nego"])

test_ssl = ('openssl s_client '
            '-servername {hostname} -connect \'{ip}:{port}\' 2>&1 </dev/null | '
            'awk \'{{ if($1 == "Server" && $2 == "public") '
            '{{ print "GOT-TLS"; }} '
            'if($2=="Connection" && $3 == "refused") print "NO-TLS"; '
            'if($1=="gethostbyname" || $0=="connect: No route to host") '
            'print "DNS-FAILURE"}}\'')

test_alpn = ('openssl s_client '
             '-alpn \'h2,http/1.1\' -servername {hostname} -connect \'{ip}:{port}\' 2>&1 </dev/null | '
             'awk \'{{if($1 == "ALPN") {{split($0, arr, ":"); '
             'print "ALPN:"arr[2];}} if($2 == "ALPN") print "NO-ALPN"; '
             'if($2=="Connection" && $3 == "refused") print "NO-TLS"; '
             'if($1=="gethostbyname" || $0=="connect: No route to host") '
             'print "DNS-FAILURE"}}\'')

test_npn = ('openssl s_client '
            '-nextprotoneg \'\' -servername {hostname} -connect \'{ip}:{port}\' 2>&1 </dev/null')

def execute_test(cmd, job_args):
    logger = logging.getLogger('tls')
    with Popen(cmd.format(**job_args), shell=True, stdout=PIPE, preexec_fn=os.setsid) as process:
        try:
            output = process.communicate(timeout=job_args['timeout'])[0]
        except TimeoutExpired:
            try:
                logger.info("Timeout for {}".format(repr(job_args)))
                os.killpg(os.getpgid(process.pid), signal.SIGINT) # kill whole process group
            except ProcessLookupError:
                logger.warning("Tried to kill process that had already completed.")
            output = process.communicate()[0]
        return output.decode('ascii')

class TLS(DesynchronizedSpider, PluggableSpider):
    """
    A PATHspider plugin for TLS testing.
    """

    def connect(self, job, pcs, config):
        ip = job[0] if not ':' in job[0] else '[' + job[0] + ']'
        job_args = {'hostname': job[2],
                    'ip': ip,
                    'port': job[1],
                    'timeout': self.args.timeout,
                   }

        connstate = False
        nego = None

        if config == 0:
            ssl_status = execute_test(test_ssl, job_args).strip()
            if ssl_status == 'GOT_TLS':
                connstate = True
        if config == 1:
            if self.args.test == 'alpn':
                alpn_status = execute_test(test_alpn, job_args).strip()
                if "ALPN" in alpn_status:
                    connstate = True
                    if ":" in alpn_status:
                        nego = alpn_status[6:]
            if self.args.test == 'npn':
                npn_status = execute_test(test_npn, job_args).split('\n')
                if len(npn_status) > 0:
                    connstate = True
                    for line in npn_status:
                        if 'advertised' in line:
                            nego = line.split(":")[1].strip()
                            break

        rec = SpiderRecord(job[0], job[1], config, job[2], job[3], config, connstate, nego)
        return rec

    def post_connect(self, job, conn, pcs, config):
        return conn

    def create_observer(self):
        return Observer()

    def merge(self, flow, res):
        flow = {"dip": res.ip,
                "dp": res.rport,
                "observed": False,
                "connstate": res.connstate,
                "config": res.config,
                "nego": res.nego,
                "host": res.host,
                "rank": res.rank,
               }
        self.outqueue.put(flow)

    @staticmethod
    def register_args(subparsers):
        parser = subparsers.add_parser('tls', help="Transport Layer Security")
        parser.set_defaults(spider=TLS)
        parser.add_argument("--timeout", default=5, type=int, help="The timeout to use for attempted connections in seconds (Default: 5)")
        parser.add_argument("--test", choices=['alpn', 'npn'], default='alpn', help="Choose to test either ALPN or NPN (Default: ALPN)")

