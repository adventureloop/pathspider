import collections
import logging
import base64
import heapq
import queue

import multiprocessing as mp

# these three for debugging
import sys
import pdb
import traceback

SHUTDOWN_SENTINEL = None

def _flow4_ids(ip):
    # FIXME keep map of fragment IDs to keys
    # FIXME link ICMP by looking at payload
    if ip.proto == 6 or ip.proto == 17 or ip.proto == 132:
        # key includes ports
        fid = ip.src_prefix.addr + ip.dst_prefix.addr + ip.data[9:10] + ip.payload[0:4]
        rid = ip.dst_prefix.addr + ip.src_prefix.addr + ip.data[9:10] + ip.payload[2:4] + ip.payload[0:2]
    else:
        # no ports, just 3-tuple
        fid = ip.src_prefix.addr + ip.dst_prefix.addr + ip.data[9:10]
        rid = ip.dst_prefix.addr + ip.src_prefix.addr + ip.data[9:10]
    return (base64.b64encode(fid), base64.b64encode(rid))

def _flow6_ids(ip6):
    # FIXME link ICMP by looking at payload
    if ip6.proto == 6 or ip6.proto == 17 or ip6.proto == 132:
        # key includes ports
        fid = ip6.src_prefix.addr + ip6.dst_prefix.addr + ip6.data[6:7] + ip6.payload[0:4]
        rid = ip6.dst_prefix.addr + ip6.src_prefix.addr + ip6.data[6:7] + ip6.payload[2:4] + ip6.payload[0:2]
    else:
        # no ports, just 3-tuple
        fid = ip6.src_prefix.addr + ip6.dst_prefix.addr + ip6.data[6:7]
        rid = ip6.dst_prefix.addr + ip6.src_prefix.addr + ip6.data[6:7]
    return (base64.b64encode(fid), base64.b64encode(rid))

PacketClockTimer = collections.namedtuple("PacketClockTimer", ("time", "fn"))

class Observer:
    """
    Wraps a packet source identified by a libtrace URI,
    parses packets to divide them into flows, passing these
    packets and flows onto a function chain to allow
    data to be associated with each flow.
    """

    def __init__(self, lturi,
                 new_flow_chain=[],
                 ip4_chain=[],
                 ip6_chain=[],
                 tcp_chain=[],
                 udp_chain=[],
                 l4_chain=[]):
        """
        Create an Observer.

        :param new_flow_chain: Array of functions to initialise new flows.
        :type new_flow_chain: array(function)
        :param ip4_chain: Array of functions to pass IPv4 headers to.
        :type ip4_chain: array(function)
        :param ip6_chain: Array of functions to pass IPv6 headers to.
        :type ip6_chain: array(function)
        :param tcp_chain: Array of functions to pass TCP headers to.
        :type tcp_chain: array(function)
        :param udp_chain: Array of functions to pass UDP headers to.
        :type udp_chain: array(function)
        :param l4_chain: Array of functions to pass other layer 4 headers to.
        :type l4_chain: array(function)
        :see also: :ref:`Observer Documentation <observer>`
        """

        # Only import this when needed
        import plt as libtrace

        # Control
        self._irq = None
        self._irq_fired = False

        # Libtrace initialization
        self._trace = libtrace.trace(lturi) # pylint: disable=no-member
        self._trace.start()
        self._pkt = libtrace.packet() # pylint: disable=no-member

        # Chains of functions to evaluate
        self._new_flow_chain = new_flow_chain
        self._ip4_chain = ip4_chain
        self._ip6_chain = ip6_chain
        self._tcp_chain = tcp_chain
        self._udp_chain = udp_chain
        self._l4_chain = l4_chain

        # Packet timer and timer queue
        self._pt = 0                   # current packet timer
        self._tq = []                  # packet timer queue (heap)

        # Flow tables
        self._active = {}
        self._expiring = {}
        self._ignored = set()

        # Emitter queue
        self._emitted = collections.deque()

        # Statistics and logging
        self._logger = logging.getLogger("observer")
        self._ct_pkt = 0
        self._ct_nonip = 0
        self._ct_shortkey = 0
        self._ct_ignored = 0
        self._ct_flow = 0

    def _interrupted(self):
        try:
            if not self._irq_fired and self._irq is not None:
                self._irq.get_nowait()
                self._irq_fired = True
        except queue.Empty:
            pass

        return self._irq_fired

    def _next_packet(self):
        # see if we're done iterating
        if not self._trace.read_packet(self._pkt):
            return False

        # see if someone told us to stop
        if self._interrupted():
            return False

        # count the packet
        self._ct_pkt += 1

        # advance the packet clock
        self._tick(self._pkt.seconds)

        # get a flow ID and associated flow record for the packet
        (fid, rec, rev) = self._get_flow()

        # don't dispatch if we don't have a record
        # (this happens for non-IP packets and flows
        #  we know we want to ignore)
        if not rec:
            return True

        keep_flow = True

        # run IP header chains
        if self._pkt.ip:
            for fn in self._ip4_chain:
                keep_flow = keep_flow and fn(rec, self._pkt.ip, rev=rev)
        elif self._pkt.ip6:
            for fn in self._ip6_chain:
                keep_flow = keep_flow and fn(rec, self._pkt.ip6, rev=rev)

        # run transport header chains
        if self._pkt.tcp:
            for fn in self._tcp_chain:
                keep_flow = keep_flow and fn(rec, self._pkt.tcp, rev=rev)
        elif self._pkt.udp:
            for fn in self._udp_chain:
                keep_flow = keep_flow and fn(rec, self._pkt.udp, rev=rev)
        else:
            for fn in self._l4_chain:
                keep_flow = keep_flow and fn(rec, self._pkt, rev=rev)

        # complete the flow if any chain function asked us to
        if not keep_flow:
            self._flow_complete(fid)

        # we processed a packet, keep going
        return True

    def _set_timer(self, delay, fid):
        # add to queue
        heapq.heappush(self._tq, PacketClockTimer(self._pt + delay,
                       self._finish_expiry_tfn(fid)))

    def _get_flow(self):
        """
        Get a flow record for the given packet.
        Create a new basic flow record
        """
        # get possible a flow IDs for the packet
        try:
            if self._pkt.ip:
                (ffid, rfid) = _flow4_ids(self._pkt.ip)
                ip = self._pkt.ip
            elif self._pkt.ip6:
                (ffid, rfid) = _flow6_ids(self._pkt.ip6)
                ip = self._pkt.ip6
            else:
                # we don't care about non-IP packets
                self._ct_nonip += 1
                return (None, None, False)
        except ValueError:
            self._ct_shortkey += 1
            return (None, None, False)

        # now look for forward and reverse in ignored, active,
        # and expiring tables.
        if ffid in self._ignored:
            return (None, None, False)
        elif rfid in self._ignored:
            return (None, None, False)
        elif ffid in self._active:
            (fid, rec) = (ffid, self._active[ffid])
            #self._logger.debug("found forward flow for "+str(ffid))
        elif ffid in self._expiring:
            (fid, rec) = (ffid, self._expiring[ffid])
            #self._logger.debug("found expiring forward flow for "+str(ffid))
        elif rfid in self._active:
            (fid, rec) = (rfid, self._active[rfid])
            #self._logger.debug("found reverse flow for "+str(rfid))
        elif rfid in self._expiring:
            (fid, rec) =  (rfid, self._expiring[rfid])
            #self._logger.debug("found expiring reverse flow for "+str(rfid))
        else:
            # nowhere to be found. new flow.
            rec = {'first': ip.seconds}
            for fn in self._new_flow_chain:
                if not fn(rec, ip):
                    # self._logger.debug("ignoring "+str(ffid))
                    self._ignored.add(ffid)
                    self._ct_ignored += 1
                    return (None, None, False)

            # wasn't vetoed. add to active table.
            fid = ffid
            self._active[ffid] = rec
            # self._logger.debug("new flow for "+str(ffid))
            self._ct_flow += 1


        # update time and return record
        rec['last'] = ip.seconds
        return (fid, rec, bool(fid == rfid))

    def _flow_complete(self, fid, delay=5):
        """
        Mark a given flow ID as complete
        """
        # move flow to expiring table
        # self._logger.debug("Moving flow " + str(fid) + " to expiring queue")
        try:
            self._expiring[fid] = self._active[fid]
        except KeyError:
            #self._logger.debug("Tried to expire an already expired flow")
            pass
        else:
            del self._active[fid]
            # set up a timer to fire to emit the flow after timeout
            self._set_timer(delay, fid)

    def _emit_flow(self, rec):
        self._emitted.append(rec)

    def _next_flow(self):
        while len(self._emitted) == 0:
            if not self._next_packet():
                return None

        return self._emitted.popleft()

    def _tick(self, pt):
        # Advance packet clock
        self._pt = pt

        # fire all timers whose time has come
        while len(self._tq) > 0 and pt > min(self._tq, key=lambda x: x.time).time:
            try:
                heapq.heappop(self._tq).fn()
            except:
                type, value, tb = sys.exc_info()
                traceback.print_exc()
                pdb.post_mortem(tb)

    def _finish_expiry_tfn(self, fid):
        """
        On expiry timer, emit the flow
        and delete it from the expiring queue
        """
        def tfn():
            if fid in self._expiring:
                self._emit_flow(self._expiring[fid])
                del self._expiring[fid]
                # self._logger.debug("emitted "+str(fid)+" on expiry")
        return tfn

    def purge_idle(self, timeout=30):
        """
        Scan the active flow table and complete any 
        flow that's been idle more than a given number of seconds.

        :param timeout idle timeout in seconds
        """

        # copy keys to a list so _flow_complete won't break the iterator
        active_fids = list(self._active.keys()):

        for fid in active_fids:
            if self._pt - self._active[fid]['last'] > timeout:
                self._flow_complete(fid)

    def flush(self):
        for fid in self._expiring:
            self._emit_flow(self._expiring[fid])
            # self._logger.debug("emitted "+str(fid)+" expiring during flush")
        self._expiring.clear()

        for fid in self._active:
            self._emit_flow(self._active[fid])
            # self._logger.debug("emitted "+str(fid)+" active during flush")
        self._active.clear()

        self._ignored.clear()

    def run_flow_enqueuer(self, flowqueue, irqueue=None, purge_interval=None):
        """
        Continually consume flows from the observer and stick them 
        into a given queue. Run this method from a thread, and read flows out of
        the resulting queue. 

        :param flowqueue queue to enqueue completed flows to
        :param irqueue queue the observer will read interrupts from. 
                       If present, writing any object to this queue 
                       will shut down the observer.
        :param purge_interval number of flows to emit between idle purges 

        """
        # store interrupt queue
        if irqueue:
            self._irq = irqueue
            self._irq_fired = None

        # Run main loop until last packet seen
        # then flush active flows and run again
        for i in range(2):
            while True:
                f = self._next_flow()
                if f:
                    flowqueue.put(f)
                    if purge_interval and (self._ct_flow % purge_interval == 0):
                        self.purge_idle()
                else:
                    self.flush()
                    break

        # log observer info on shutdown
        self._logger.info(
                ("processed %u packets "+
                "(%u dropped, %u short, %u non-ip) "+
                "into %u flows (%u ignored)") % (
                    self._ct_pkt, self._trace.pkt_drops(),
                    self._ct_shortkey, self._ct_nonip,
                    self._ct_flow, self._ct_ignored))

        flowqueue.put(SHUTDOWN_SENTINEL)

def extract_ports(ip):
    if ip.udp:
        return (ip.udp.src_port, ip.udp.dst_port)
    elif ip.tcp:
        return (ip.tcp.src_port, ip.tcp.dst_port)
    else:
        return (None, None)

def basic_flow(rec, ip):
    """
    New flow function that sets up basic flow information
    """

    # Extract addresses and ports
    (rec['sip'], rec['dip'], rec['proto']) = (str(ip.src_prefix), str(ip.dst_prefix), ip.proto)
    (rec['sp'], rec['dp']) = extract_ports(ip)

    # Initialize counters
    rec['pkt_fwd'] = 0
    rec['pkt_rev'] = 0
    rec['oct_fwd'] = 0
    rec['oct_rev'] = 0

    # we want to keep this flow
    return True

def basic_count(rec, ip, rev):
    """
    Packet function that counts packets and octets per flow
    """

    if rev:
        rec["pkt_rev"] += 1
        rec["oct_rev"] += ip.size
    else:
        rec["pkt_fwd"] += 1
        rec["oct_fwd"] += ip.size

    return True

def simple_observer(lturi):
    return Observer(lturi,
                    new_flow_chain=[basic_flow],
                    ip4_chain=[basic_count],
                    ip6_chain=[basic_count])
