
TCP_CWR = 0x80
TCP_ECE = 0x40
TCP_ACK = 0x10
TCP_SYN = 0x02

TCP_SEC = ( TCP_SYN | TCP_ECE | TCP_CWR )
TCP_SAEW = (TCP_SYN | TCP_ACK | TCP_ECE | TCP_CWR)
TCP_SAE = (TCP_SYN | TCP_ACK | TCP_ECE)

def tcp_setup(rec, ip):
    rec['fwd_syn_flags'] = None
    rec['rec_syn_flags'] = None

    rec['fwd_fin'] = False
    rec['rev_fin'] = False
    rec['fwd_rst'] = False
    rec['rev_rst'] = False

    rec['tcp_connected'] = False

    return True

def tcp_handshake(rec, tcp, rev):
    flags = tcp.flags

    if flags & TCP_SYN:
        if rev == 0:
            rec['fwd_syn_flags'] = flags
        if rev == 1:
            rec['rev_syn_flags'] = flags

    # TODO: This test could perhaps be improved upon.
    # This test is intended to catch the completion of the 3WHS.
    if (not tcp.connected and rev == 0 and
       rec['fwd_syn_flags'] is not None and
       rec['rev_syn_flags'] is not None and
       flags & TCP_ACK):
        rec['tcp_connected'] = True

    return True

def tcp_complete(rec, tcp, rev): # pylint: disable=W0612,W0613
    if tcp.fin_flag and rev:
        rec['rev_fin'] = True
    if tcp.fin_flag and not rev:
        rec['fwd_fin'] = True
    if tcp.rst_flag and rev:
        rec['rev_rst'] = True
    if tcp.rst_flag and not rev:
        rec['fwd_rst'] = True

    return not ( ( rec['fwd_fin'] and rec['rev_fin'] ) or
                 rec['fwd_rst'] or rec['rev_rst'] )

