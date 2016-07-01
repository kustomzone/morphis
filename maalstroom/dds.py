# Copyright (c) 2016  Sam Maloney.
# License: GPL v2.

import llog

import asyncio
from datetime import datetime
import logging
import os

import consts
from db import User, Axon
from dmail import DmailEngine
import dpush
import enc
import maalstroom.dmail as dmail
import maalstroom.templates as templates
import mbase32
from morphisblock import MorphisBlock
import targetedblock as tb
from mutil import fia, hex_dump, make_safe_for_html_content, utc_datetime
import sshtype

log = logging.getLogger(__name__)

s_dds = ".dds"
s_dds_len = len(".dds")

@asyncio.coroutine
def serve_get(dispatcher, rpath):
    log.info("Service .dds request.")

    req = rpath[s_dds_len:]

    if req == "" or req == "/":
        random_id_enc = mbase32.encode(os.urandom(consts.NODE_ID_BYTES))

        template = templates.dds_main[0]
        template = template.format(random_id_enc=random_id_enc)

        dispatcher.send_content(template)
        return
    elif req == "/style.css":
        template = templates.dds_css[0]

        dispatcher.send_content(template, content_type="text/css")
        return
    elif req.startswith("/images/"):
        dispatcher.send_content(templates.dds_imgs[req[8:]])
        return
    elif req == "/axon/create":
        # Render the Create Axon (Targeted or not) page.
        yield from _process_axon_create(dispatcher, req[5:])
        return
    elif req.startswith("/axon/grok/"):
        # Render the Grok View; which shows the Axon, SynapseS and Synapse
        # create form.
        yield from _process_axon_grok(dispatcher, req[11:])
        return
    elif req.startswith("/axon/read/"):
        # Render an individual Axon.
        yield from _process_axon_read(dispatcher, req[11:])
        return
    elif req.startswith("/axon/synapses/"):
        # Scan for and render SynapseS connected to the requested Axon.
        yield from _process_axon_synapses(dispatcher, req[15:])
        return
    elif req.startswith("/synapse/create/"):
        # Render the Create Synapse entry form.
        yield from _process_synapse_create(dispatcher, req[16:])
        return

    dispatcher.send_error("request: {}".format(req), errcode=400)

@asyncio.coroutine
def serve_post(dispatcher, rpath):
    assert rpath.startswith(s_dds)

    log.info("Service .dds post.")

    req = rpath[s_dds_len:]

    if req == "/synapse/create":
        yield from _process_synapse_create_post(dispatcher, req)
        return

    dispatcher.send_error("request: {}".format(req), errcode=400)

@asyncio.coroutine
def _process_axon_create(dispatcher, req):
    template = templates.dds_axon[0]
    template = template.format(\
        message_text="",
        csrf_token=dispatcher.client_engine.csrf_token,
        delete_class="display_none")

    dispatcher.send_content([template, req])

@asyncio.coroutine
def _process_axon_grok(dispatcher, req):
    if req.startswith("@"):
        #TODO: Come up with a formal spec. We should probably deal with
        # unprintable characters by merging them, Etc.
        str_id = req[1:].encode().lower()
        key = enc.generate_ID(str_id)
        significant_bits = None
    else:
        key, significant_bits = dispatcher.decode_key(req)

        if not key:
            dispatcher.send_error(\
                "Invalid encoded key: [{}].".format(req), 400)
            return

    if significant_bits:
        # Support prefix keys.
        key = yield from dispatcher.fetch_key(key, significant_bits)

        if not key:
            return

    msg = "<iframe src='morphis://.dds/axon/read/{key}'"\
        " style='height: 10em; width: 100%; border: 1;'"\
        " seamless='seamless'></iframe><iframe"\
        " src='morphis://.dds/axon/synapses/{key}#new'"\
        " style='height: calc(100% - 17.5em); width: 100%; border: 0;'"\
        " seamless='seamless'></iframe><iframe"\
        " src='morphis://.dds/synapse/create/{key}'"\
        " style='height: 7em; width: 100%; border: 1;'"\
        " seamless='seamless'></iframe>"\
            .format(key=mbase32.encode(key))

    dispatcher.send_content(msg)
    return

@asyncio.coroutine
def _process_axon_read(dispatcher, req):
    p0 = req.find('/')

    if p0 > -1:
        # Then the request is for a TargetedBlock.
        key = mbase32.decode(req[:p0])
        target_key = mbase32.decode(req[p0+1:])
    else:
        # Then the request is not for a TargetedBlock.
        key = mbase32.decode(req)
        target_key = None

    axon = yield from _load_axon(dispatcher, key, target_key)

    axon_id = axon.id if axon else None

    if not axon or axon.data is None:
        if target_key:
            data_rw =\
                yield from\
                    dispatcher.node.chord_engine.tasks.send_get_targeted_data(\
                        bytes(key), target_key=target_key)
        else:
            data_rw =\
                yield from\
                    dispatcher.node.chord_engine.tasks.send_get_data(\
                        bytes(key))

        data = data_rw.data
    else:
        data = axon.data

    if not data:
        dispatcher.send_content("Not found on the network at the moment.")
        return

    yield from _save_axon(dispatcher, key, target_key, data, axon_id)

    if not data.startswith(MorphisBlock.UUID):
        # Plain data, return it!
        key_enc = mbase32.encode(key)

        content = yield from _format_axon(dispatcher.node, data, key, key_enc)

        template = templates.dds_synapse_view[0]

        template = template.format(\
            key=key_enc, content=content)

        msg = "<head><link rel='stylesheet' href='morphis://.dds/style.css'></link></head><body style='height: 90%; padding:0;margin:0;'>{}</body>".format(template)

        acharset = dispatcher.get_accept_charset()
        dispatcher.send_content(\
            msg, content_type="text/html; charset={}".format(acharset))
        return

    # We assume it is a Dmail if it is a MorphisBlock.
    dispatcher.send_content(hex_dump(data))

@asyncio.coroutine
def _process_axon_synapses(dispatcher, axon_addr_enc):
    axon_addr = mbase32.decode(axon_addr_enc)

#    dispatcher.send_partial_content(\
#        "<head><meta target='_self' http-equiv='refresh' content='15'></meta>"\
#            "</head><body>", True)
    dispatcher.send_partial_content(templates.dds_axon_synapses_start[0], True)

    first = False # Get rid of this now as we always open with <body>.

    def dbcall():
        with dispatcher.node.db.open_session() as sess:
            q = sess.query(Axon)\
                .filter(Axon.target_key == axon_addr)\
                .order_by(Axon.first_seen)

            return q.all()

    axons = yield from dispatcher.loop.run_in_executor(None, dbcall)

    loaded = {}

    for axon in axons:
#        msg = "<div style='height: 5.5em; width: 100%; overflow: hidden;'>"\
#            "{}</div>\n".format(_format_axon(axon.data, axon.key))

        content =\
            yield from _format_axon(\
                dispatcher.node, axon.data, axon.key, mbase32.encode(axon.key))

        template = templates.dds_synapse_view[0]

        template = template.format(\
            key=axon_addr_enc,\
            content=content)

        dispatcher.send_partial_content(template, first)

        first = False
        loaded[axon.key] = True

    dispatcher.send_partial_content("<hr id='new'/>", first)

    @asyncio.coroutine
    def cb(key):
        nonlocal first

        key_enc = mbase32.encode(key)

        if loaded.get(bytes(key)):
            log.info("Skipping already loaded synapse for key=[{}]."\
                .format(key_enc))
            return

        msg = "<iframe src='morphis://.dds/axon/read/{key}/{target_key}' style='height: 15em; width: 100%; border: 0;' seamless='seamless'></iframe>\n"\
            .format(key=key_enc,\
                target_key=axon_addr_enc)

        dispatcher.send_partial_content(msg)

        first = False

    dp = dpush.DpushEngine(dispatcher.node)

    yield from dp.scan_targeted_blocks(axon_addr, 8, cb)

    if first:
        dispatcher.send_partial_content("Nothing found yet.</body>")

    dispatcher.send_partial_content(\
        "<div>Last refreshed: {}</div><span id='end' style='color: gray'/>"\
        "</body></html>"\
            .format(utc_datetime()))

    dispatcher.end_partial_content()

@asyncio.coroutine
def _process_synapse_create(dispatcher, target_addr):
    if dispatcher.handle_cache(target_addr):
        return

    template = templates.dds_create_synapse[0]

    template = template.format(\
        csrf_token=dispatcher.client_engine.csrf_token,\
        message_text="",\
        target_addr=target_addr)

    dispatcher.send_content(template)

@asyncio.coroutine
def _process_synapse_create_post(dispatcher, req):
    dd = yield from dispatcher.read_post()
    if not dd:
        dispatcher.send_error("request: {}".format(req), errcode=400)
        return

    content = fia(dd["content"])
    content2 = fia(dd.get("content2"))

    if not content:
        content = content2
    elif content2:
        content = content + "\r\n" + content2

    if not content:
        dispatcher.send_error("No content.", errcode=400)
        return

    key = None

    def key_callback(akey):
        nonlocal key
        key = akey

    yield from\
        dispatcher.node.chord_engine.tasks.send_store_data(\
            content.encode(), store_key=True, key_callback=key_callback)

    target_addr = fia(dd["target_addr"])

    if not target_addr:
        resp =\
            "Resulting&nbsp;<a href='morphis://.dds/axon/read/{axon_addr}'>"\
                "Axon</a>&nbsp;Address:<br/>{axon_addr}"\
                     .format(axon_addr=mbase32.encode(key))

        dispatcher.send_content(resp)
        return

    target_addr = mbase32.decode(target_addr)

    synapse = tb.Synapse.for_target(target_addr, key)

    yield from\
        dispatcher.node.chord_engine.tasks.send_store_synapse(\
            synapse, store_key=True, key_callback=key_callback)

    resp =\
        "Resulting&nbsp;<a href='morphis://.dds/axon/read/{synapse_addr}/"\
            "{target_addr}'>Synapse</a>&nbsp;Address:<br/>{synapse_addr}"\
                 .format(\
                    synapse_addr=mbase32.encode(key),\
                    target_addr=mbase32.encode(target_addr))

    dispatcher.send_content(resp)

@asyncio.coroutine
def _format_axon(node, data, key, key_enc):
    try:
        result = __format_post(data)
    except UnicodeDecodeError:
        synapse = tb.Synapse(data)

        data_rw = yield from\
                node.chord_engine.tasks.send_get_data(synapse.source_key)

        if not data_rw or data_rw.data is None:
            return "<NOT FOUND>"

        result = __format_post(data_rw.data)

    return result\
        + "<div style='font-family: monospace; font-size: 8pt;"\
            "color: #a0a0ff; position: absolute; bottom: 0.3em;"\
            "right: 0.3em;'>{}</div>".format(key_enc[:32])

def __format_post(data):
    fr = data.find(b'\r')
    fn = data.find(b'\n')

    if fr == -1 and fn == -1:
        return "<h3>{}</h3>".format(make_safe_for_html_content(data))

    if fr == -1:
        end = fn
        start = end + 1
    elif fn == -1:
        end = fr
        start = end + 1
    else:
        end = fr
        start = end + 2

    return "<h3 style='padding:0;margin:0;'>{}</h3>"\
        "<pre style='color: gray; padding:0;margin:0;'>{}</pre>"\
            .format(\
                data[:end].decode(), make_safe_for_html_content(data[start:]))

@asyncio.coroutine
def _load_axon(dispatcher, key, target_key):
    def dbcall():
        with dispatcher.node.db.open_session() as sess:
            q = sess.query(Axon).filter(Axon.key == key)

            return q.first()

    return (yield from dispatcher.loop.run_in_executor(None, dbcall))

@asyncio.coroutine
def _save_axon(dispatcher, key, target_key=None, data=None, dbid=None):
    def dbcall():
        with dispatcher.node.db.open_session() as sess:
            axon = None

            if dbid:
                axon = sess.query(Axon).get(dbid)

            if not axon:
                axon = Axon()
                axon.key = key
                axon.first_seen = utc_datetime()

                if target_key:
                    axon.target_key = target_key

                sess.add(axon)

            if data:
                axon.data = data

            sess.commit()

            return axon

    return (yield from dispatcher.loop.run_in_executor(None, dbcall))
