# Copyright (c) 2016  Sam Maloney.
# License: GPL v2.

import llog

import asyncio
from concurrent import futures
from datetime import datetime
import json
import logging
from urllib.parse import parse_qs
import os

from sqlalchemy import or_

import consts
from db import User, DdsPost
from dmail import DmailEngine
from dds import DdsEngine
import dpush
import enc
import maalstroom.dmail as dmail
import maalstroom.templates as templates
import mbase32
from morphisblock import MorphisBlock
from mutil import fia, hex_dump, make_safe_for_html_content
import mutil
import rsakey
import synapse as syn
import targetedblock as tb
import sshtype

log = logging.getLogger(__name__)

S_DDS = ".dds"

class MaalstroomRequest(object):
    def __init__(self, dispatcher, service, rpath):
        assert type(service) is str
        self.dispatcher = dispatcher
        self.service = service
        self.path, sep, self._query = rpath.partition('?')
        self.req = self.path[len(service):]

        if self._query:
            self.qdict = parse_qs(self._query, keep_blank_values=True)
            self._query = '?' + self._query
        else:
            self.qdict = {}

        self._modified = False # True means self.query needs rebuild.

    @property
    def modified(self):
        return self._modified

    def set_modified(self):
        self._modified = True

class DdsRequest(MaalstroomRequest):
    def __init__(self, dispatcher, rpath):
        super().__init__(dispatcher, ".dds", rpath)

        self._ident_enc = None
        self.__ident_enc = False
        self._ident = None
        self.__ident = False

    @property
    def ident_enc(self):
        if self.__ident_enc:
            return self._ident_enc

        self.__ident_enc = True
        self._ident_enc = fia(self.qdict.get("ident"))

        return self._ident_enc

    @property
    def ident(self):
        if self.__ident:
            return self._ident

        self.__ident = True
        if self.ident_enc:
            self._ident = mbase32.decode(self.ident_enc)
        elif self._ident_enc is None:
            self._ident = None # Default.
        else:
            assert self._ident_enc == ""
            self._ident = b"" # Anonymous.

        return self._ident

    @ident.setter
    def ident(self, value):
        self.set_modified()

        self._ident = value

        if value is None:
            self._ident_enc = None
            return
        elif not value:
            assert value == b""
            self._ident_enc = ""
            return

        self._ident_enc = mbase32.encode(value)
        self.ident_enc_ = True

    @ident_enc.setter
    def ident_enc(self, value):
        self.set_modified()

        self._ident_enc = value

        if value is None:
            self._ident = None
            return
        elif not value:
            assert value == ""
            self._ident = b""
            return

        self._ident = mbase32.decode(value)
        self.ident_ = True

    @property
    def query(self):
        if self._query and not self.modified:
            return self._query

        if self.ident_enc:
            self._query = "?ident={}".format(self.ident_enc)
        else:
            self._query = ""

        return self._query

@asyncio.coroutine
def serve_get(dispatcher, rpath):
    log.info("Service .dds request.")

    mr = DdsRequest(dispatcher, rpath)
    req = mr.req

    if req == "" or req == "/":
        yield from _process_root(mr)
    elif req == "/style.css":
        dispatcher.send_content(templates.dds_css[0], content_type="text/css")
    elif req.startswith("/images/"):
        dispatcher.send_content(templates.dds_imgs[req[8:]])
    elif req == "/axon/create":
        # Render the Create Axon (Targeted or not) page.
        yield from _process_axon_create(dispatcher, req[5:])
    elif req.startswith("/axon/grok/"):
        # Render the Grok View; which shows the Axon, SynapseS and Synapse
        # create form.
        yield from _process_axon_grok(mr)
    elif req.startswith("/axon/read/"):
        # Render an individual Axon.
        yield from _process_axon_read(dispatcher, req[11:])
    elif req.startswith("/axon/synapses/"):
        # Scan for and render SynapseS connected to the requested Axon.
        yield from _process_axon_synapses(dispatcher, req[15:])
    elif req.startswith("/synapse/create/"):
        # Render the Create Synapse entry form.
        yield from _process_synapse_create(mr)
    else:
        dispatcher.send_error("request: {}".format(req), errcode=400)

@asyncio.coroutine
def serve_post(dispatcher, rpath):
    assert rpath.startswith(S_DDS)

    log.info("Service .dds post.")

    req = rpath[len(S_DDS):]

    if req == "/synapse/create":
        yield from _process_synapse_create_post(dispatcher, req)
        return

    dispatcher.send_error("request: {}".format(req), errcode=400)

@asyncio.coroutine
def _process_root(req):
    random_id_enc = mbase32.encode(os.urandom(consts.NODE_ID_BYTES))

    # Determine ident.
    if req.ident_enc is None:
        dmail_address = yield from dmail._load_default_dmail_address(\
            req.dispatcher, fetch_keys=True)
        if dmail_address:
            req.ident = dmail_address.site_key

    template = templates.dds_main[0]
    template = template.format(random_id_enc=random_id_enc, query=req.query)

    available_idents = yield from dmail.render_dmail_addresses(\
        req.dispatcher, req.ident, use_key_as_id=True)

    template2 = templates.dds_identbar[0]
    template2 = template2.format(\
        current_ident=req.ident_enc, available_idents=available_idents)

    template = template2 + template

    wrapper = templates.dds_wrapper[0]
    wrapper =\
        wrapper.format(title="MORPHiS Maalstroom DDS", child=template)

    req.dispatcher.send_content(wrapper)

@asyncio.coroutine
def _process_axon_create(dispatcher, req):
    template = templates.dds_axon[0]
    template = template.format(\
        message_text="",
        csrf_token=dispatcher.client_engine.csrf_token,
        delete_class="display_none")

    dispatcher.send_content([template, req])

@asyncio.coroutine
def _process_axon_grok(req):
    arg = req.req[11:]

    if arg.startswith("@"):
        key = DdsEngine.calc_key_for_channel(arg[1:])
        significant_bits = None
    else:
        key, significant_bits = req.dispatcher.decode_key(arg)

        if not key:
            req.dispatcher.send_error(\
                "Invalid encoded key: [{}].".format(arg), 400)
            return

    if significant_bits:
        # Support prefix keys.
        key = yield from req.dispatcher.fetch_key(key, significant_bits)

        if not key:
            return

    msg = "<iframe src='morphis://.dds/axon/read/{key}{query}'"\
        " style='height: 8em; width: 100%; border: 0;'"\
        " seamless='seamless'></iframe><iframe"\
        " src='morphis://.dds/axon/synapses/{key}{query}#new'"\
        " style='height: calc(100% - 19em); width: 100%; border: 0;'"\
        " seamless='seamless'></iframe><iframe"\
        " src='morphis://.dds/synapse/create/{key}{query}'"\
        " style='height: 8.5em; width: 100%; border: 0;'"\
        " seamless='seamless'></iframe>"\
            .format(key=mbase32.encode(key), query=req.query)

    req.dispatcher.send_content(msg)
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

    post = yield from retrieve_post(dispatcher.node, key, target_key)

    if not post:
        dispatcher.send_content("Not found on the network at the moment.")
        return

    key_enc = mbase32.encode(key)

    content = yield from _format_axon(dispatcher.node, post.data, key, key_enc)

    timestr = mutil.format_human_no_ms_datetime(post.timestamp)

    template = templates.dds_synapse_view[0]
    template = template.format(\
        key=key_enc,\
        signing_key="",\
        signer="<TODO>",\
        content=content,\
        timestamp=timestr)

    msg = "<head><link rel='stylesheet' href='morphis://.dds/style.css'>"\
        "</link></head><body style='height: 80%; padding:0;margin:0;'>{}"\
        "</body>"\
            .format(template)

    content_type = "text/html; charset={}"\
        .format(dispatcher.get_accept_charset())

    dispatcher.send_content(msg, content_type=content_type)
    return

@asyncio.coroutine
def _process_axon_synapses(dispatcher, axon_addr_enc):
    axon_addr = mbase32.decode(axon_addr_enc)

    dispatcher.send_partial_content(templates.dds_axon_synapses_start[0], True)

    loaded = {}

    @asyncio.coroutine
    def process_post(post):
        if type(post) is syn.Synapse:
            key = post.synapse_key
            post = yield from retrieve_post(dispatcher.node, post)
        elif type(post) is DdsPost:
            key = post.synapse_key
            if not key:
                key = post.data_key
        else:
            assert type(post) in (bytes, bytearray)
            key = post
            post = yield from retrieve_post(dispatcher.node, post, axon_addr)

        if not post:
            if log.isEnabledFor(logging.INFO):
                log.info(\
                    "Post data not found for found key [{}]."\
                        .format(mbase32.encode(key)))
            return

#        if post.synapse_key:
#            loaded.setdefault(post.synapse_key, True)
#        if post.synapse_pow:
#            loaded.setdefault(post.synapse_pow, True)
#        loaded.setdefault(post.data_key) = True

        key_enc = mbase32.encode(key)

        content =\
            yield from _format_axon(dispatcher.node, post.data, key, key_enc)

        timestr = mutil.format_human_no_ms_datetime(post.timestamp)

        signing_key = post.signing_key if post.signing_key else ""
        signer_name =\
            yield from dmail.get_contact_name(dispatcher.node, signing_key)

        signing_key_enc = mbase32.encode(post.signing_key)

        if signing_key and signer_name == signing_key_enc:
            data_rw = yield from dispatcher.node.engine.tasks.send_get_data(\
                signing_key, force_cache=True)

            if data_rw:
                json_bytes = data_rw.data
                if json_bytes:
                    name = json.loads(json_bytes.decode()).get("name")
                    if name:
                        signer_name = make_safe_for_html_content(name)
                        log.info("Using Dsite name=[{}].".format(signer_name))

        template = templates.dds_synapse_view[0]
        template =\
            template.format(\
                key=key_enc,\
                signing_key=signing_key_enc,\
                signer=signer_name,\
                content=content,\
                timestamp=timestr)

        dispatcher.send_partial_content(template)

    def dbcall():
        with dispatcher.node.db.open_session(True) as sess:
            q = sess.query(DdsPost)\
                .filter(DdsPost.target_key == axon_addr)\
                .order_by(DdsPost.timestamp)

            return q.all()

    posts = yield from dispatcher.loop.run_in_executor(None, dbcall)

    for post in posts:
        key = post.synapse_pow if post.synapse_pow else post.data_key
        loaded.setdefault(key, True)
        yield from process_post(post)

    dispatcher.send_partial_content("<hr id='new'/>")

    new_tasks = []

    @asyncio.coroutine
    def cb(key):
        nonlocal new_tasks

        if type(key) is bytearray:
            key = bytes(key)

        if loaded.get(key):
            if log.isEnabledFor(logging.INFO):
                log.info("Skipping already loaded TargetedBlock/Synapse for"\
                    " key=[{}].".format(mbase32.encode(key)))
            return

        loaded.setdefault(key, True)

        new_tasks.append(\
            asyncio.async(\
                process_post(key),\
                loop=dispatcher.node.loop))

    @asyncio.coroutine
    def cb2(data_rw):
        nonlocal new_tasks

        for synapse in data_rw.data:
            if loaded.get(bytes(synapse.synapse_pow)):
                if log.isEnabledFor(logging.INFO):
                    log.info("Skipping already loaded Synapse for key=[{}]."\
                        .format(mbase32.encode(synapse.synapse_pow)))
                continue

            loaded.setdefault(synapse.synapse_pow, True)

            new_tasks.append(\
                asyncio.async(\
                    process_post(synapse),\
                    loop=dispatcher.node.loop))

    dp = dpush.DpushEngine(dispatcher.node)
    new_tasks.append(asyncio.async(dp.scan_targeted_blocks(axon_addr, 8, cb)))

    yield from dispatcher.node.engine.tasks.send_get_synapses(\
        axon_addr, result_callback=cb2, retry_factor=25)

    if new_tasks:
        yield from asyncio.wait(\
            new_tasks,\
            loop=dispatcher.node.loop,\
            return_when=futures.ALL_COMPLETED)

    dispatcher.send_partial_content(\
        "<div>Last refreshed: {}</div><span id='end' style='color: gray'/>"\
        "</body></html>"\
            .format(mutil.utc_datetime()))

    dispatcher.end_partial_content()

@asyncio.coroutine
def _process_synapse_create(req):
    target_addr = req.req[16:]
    if req.dispatcher.handle_cache(target_addr):
        return

    if not req.ident:
        dmail_address =\
            yield from dmail._load_default_dmail_address(req.dispatcher)
        req.ident = dmail_address.site_key

    ident_name =\
        yield from dmail.get_contact_name(req.dispatcher.node, req.ident)
    if ident_name == req.ident_enc or not req.ident:
        ident_str = ident_name
    else:
        ident_str = "{} ({})".format(ident_name, req.ident_enc)

    template = templates.dds_create_synapse[0]
    template = template.format(\
        csrf_token=req.dispatcher.client_engine.csrf_token,\
        message_text="",\
        target_addr=target_addr,\
        ident=req.ident_enc,\
        ident_str=ident_str,\
        query=req.query)

#    template =\
#        templates.dds_wrapper[0].format(title="DDS Post Box", child=template)

    req.dispatcher.send_content(template)

@asyncio.coroutine
def _process_synapse_create_post(dispatcher, req):
    dd = yield from dispatcher.read_post()
    if not dd:
        dispatcher.send_error("request: {}".format(req), errcode=400)
        return

    if not dispatcher.check_csrf_token(dd["csrf_token"][0]):
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

    content_key = None
    content_key_ready = asyncio.Event()

    def key_callback(akey):
        nonlocal content_key

        content_key = akey
        content_key_ready.set()

        if log.isEnabledFor(logging.INFO):
            log.info("content_key=[{}].".format(mbase32.encode(content_key)))

    @asyncio.coroutine
    def store_content():
        storing_nodes = 0
        for retry in range(10, 50, 5):
            storing_nodes += yield from\
                dispatcher.node.chord_engine.tasks.send_store_data(\
                    content.encode(),\
                    store_key=True,\
                    key_callback=key_callback,\
                    retry_factor=retry)

            if storing_nodes >= 5:
                if log.isEnabledFor(logging.INFO):
                    log.info("Stored content; storing_nodes=[{}]."\
                        .format(storing_nodes))
                break

        return storing_nodes

    content_task = asyncio.async(store_content(), loop=dispatcher.loop)

    yield from content_key_ready.wait()

    target_addr = fia(dd["target_addr"])

    if not target_addr:
        resp =\
            "Resulting&nbsp;<a href='morphis://.dds/axon/read/{axon_addr}'>"\
                "Axon</a>&nbsp;Address:<br/>{axon_addr}"\
                     .format(axon_addr=mbase32.encode(content_key))

        dispatcher.send_content(resp)
        return

    target_addr = mbase32.decode(target_addr)

    synapse = syn.Synapse.for_target(target_addr, content_key)

    ident_enc = fia(dd["ident"])
    if ident_enc:
        ident_addr = mbase32.decode(ident_enc)
        ident_dmail_address = yield from\
            dmail.load_dmail_address(dispatcher.node, site_key=ident_addr)
        signing_key =\
            rsakey.RsaKey(privdata=ident_dmail_address.site_privatekey)
        synapse.key = signing_key

    yield from dispatcher.node.engine.tasks.send_store_synapse(synapse)

    storing_nodes =\
        yield from asyncio.wait_for(content_task, None, loop=dispatcher.loop)

    if storing_nodes < 5:
        log.warning(\
            "Only [{}] storing nodes for content.".format(storing_nodes))

    resp =\
        "Resulting&nbsp;<a href='morphis://.dds/axon/read/{synapse_addr}/"\
            "{target_addr}'>Synapse</a>&nbsp;Address:<br/>{synapse_addr}"\
                 .format(\
                    synapse_addr=mbase32.encode(synapse.synapse_pow),\
                    target_addr=mbase32.encode(target_addr))

    dispatcher.send_content(resp)

@asyncio.coroutine
def _format_axon(node, data, key, key_enc=None):
    result = __format_post(data)

    if not key_enc:
        key_enc = mbase32.encode(key)

    return result\
        + "<div style='font-family: monospace; font-size: 8pt;"\
            "color: #80C9D1; position: absolute; bottom: 0.3em;"\
            "padding-right: 10px; padding-bottom: 5px;"\
            "right: 0.3em;'>{}</div>".format(key_enc[:32])

def __format_post(data):
    fr = data.find(b'\r')
    fn = data.find(b'\n')

    if fr == -1 and fn == -1:
        return "{}".format(make_safe_for_html_content(data))

    if fr == -1:
        end = fn
        start = end + 1
    elif fn == -1:
        end = fr
        start = end + 1
    else:
        end = fr
        start = end + 2

    return "{}<br/>" \
        "{}"\
            .format(\
                data[:end].decode(), make_safe_for_html_content(data[start:]))

#TODO: Move to DdsEngine.

@asyncio.coroutine
def retrieve_post(node, key, target_key=None):
    synapse = None
    if type(key) is syn.Synapse:
        assert not target_key
        synapse = key
        key = synapse.synapse_key
        target_key = synapse.target_key

    post = yield from _load_dds_post(node, key)

    if post:
        return post

    if not target_key:
        # Plain static data.
        data_rw = yield from\
            node.chord_engine.tasks.send_get_data(bytes(key))

        obj = None
    else:
        # TargetedBlock or Synapse.
        if synapse:
            obj = synapse
        else:
            data_rw =\
                yield from node.chord_engine.tasks.send_get_targeted_data(\
                    bytes(key), target_key=target_key)
            obj = data_rw.object

        if obj:
            if type(obj) is syn.Synapse:
                data_rw = yield from\
                    node.chord_engine.tasks.send_get_data(obj.source_key)
            else:
                assert type(obj) is tb.TargetedBlock, type(obj)
                data_rw.data = data_rw.data[tb.TargetedBlock.BLOCK_OFFSET:]

    if not data_rw.data:
        return None

    # Cache the 'post' locally.
    post = yield from _save_dds_post(node, key, target_key, obj, data_rw.data)

    return post

@asyncio.coroutine
def _load_dds_post(node, key):
    def dbcall():
        with node.db.open_session(True) as sess:
            q = sess.query(DdsPost).filter(
                or_(\
                    DdsPost.synapse_key == key,\
                    DdsPost.synapse_pow == key,\
                    DdsPost.data_key == key))

            return q.first()

    return (yield from node.loop.run_in_executor(None, dbcall))

@asyncio.coroutine
def _save_dds_post(node, key, target_key, obj, data):
    def dbcall():
        with node.db.open_session() as sess:
            post = DdsPost()

            post.first_seen = mutil.utc_datetime()
            post.data = data

            if obj:
                assert target_key
                post.target_key = target_key

                if type(obj) is syn.Synapse:
                    post.synapse_key = obj.synapse_key
                    post.synapse_pow = obj.synapse_pow
                    post.data_key = obj.source_key
                    if obj.is_signed():
                        post.signing_key = obj.signing_key
                    post.timestamp = mutil.utc_datetime(obj.timestamp)
                else:
                    assert type(obj) is tb.TargetedBlock, type(obj)
                    post.data_key = post.synapse_pow = key
                    post.timestamp = mutil.utc_datetime(0)
            else:
                post.data_key = key
                post.timestamp = post.first_seen

            sess.add(post)

            sess.commit()

            # Make sure data is loaded for use by caller.
            len(post.data)

            sess.expunge_all()

            return post

    return (yield from node.loop.run_in_executor(None, dbcall))