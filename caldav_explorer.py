# /// script
# requires-python = ">=3.10"
# dependencies = ["caldav>=0.11"]
# ///

from __future__ import annotations

import queue
import textwrap
import threading
import tkinter as tk
import traceback
import xml.dom.minidom
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from tkinter import font as tkfont
from tkinter import ttk
from urllib.parse import unquote, urljoin, urlparse

import caldav

NS = {
    "D": "DAV:",
    "C": "urn:ietf:params:xml:ns:caldav",
    "CS": "http://calendarserver.org/ns/",
    "A": "http://apple.com/ns/ical/",
}

LIST_BODY = """<?xml version="1.0" encoding="utf-8"?>
<D:propfind xmlns:D="DAV:">
  <D:prop>
    <D:resourcetype/>
    <D:displayname/>
    <D:getcontenttype/>
    <D:getcontentlength/>
  </D:prop>
</D:propfind>"""

COLLECTION_BODY = """<?xml version="1.0" encoding="utf-8"?>
<D:propfind xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav"
            xmlns:CS="http://calendarserver.org/ns/" xmlns:A="http://apple.com/ns/ical/">
  <D:prop>
    <D:displayname/>
    <D:resourcetype/>
    <C:calendar-description/>
    <A:calendar-color/>
    <C:supported-calendar-component-set/>
    <C:max-resource-size/>
    <CS:getctag/>
    <D:sync-token/>
    <D:owner/>
    <D:current-user-privilege-set/>
  </D:prop>
</D:propfind>"""

PRINCIPAL_BODY = """<?xml version="1.0" encoding="utf-8"?>
<D:propfind xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
  <D:prop>
    <D:displayname/>
    <D:resourcetype/>
    <D:current-user-principal/>
    <D:principal-URL/>
    <C:calendar-home-set/>
    <C:calendar-user-address-set/>
    <C:schedule-inbox-URL/>
    <C:schedule-outbox-URL/>
  </D:prop>
</D:propfind>"""

TYPE_NOTES = {
    "principal": "A principal represents you (a user) on the server. Its "
    "properties point to where your data lives, most importantly the "
    "calendar-home-set.",
    "calendar": "A calendar collection. Each file inside is one calendar "
    "object resource (.ics), usually holding a single VEVENT, VTODO or VJOURNAL.",
    "schedule-inbox": "The scheduling inbox (RFC 6638). Meeting invitations "
    "and replies sent to you land here before being processed.",
    "schedule-outbox": "The scheduling outbox (RFC 6638). Used by clients to "
    "send invitations and free/busy queries. Usually appears empty.",
    "addressbook": "A CardDAV address book. Files inside are vCards (.vcf).",
    "collection": "A plain WebDAV collection (folder). The calendar home is "
    "usually one of these, containing your calendars.",
}


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def strip_slash(path: str) -> str:
    return path.rstrip("/") or "/"


def prop_value(el: ET.Element) -> str:
    if len(el) == 0:
        return (el.text or "").strip()
    parts = []
    for leaf in el.iter():
        if len(leaf) or leaf is el:
            continue
        if "name" in leaf.attrib:
            parts.append(leaf.attrib["name"])
        elif leaf.text and leaf.text.strip():
            parts.append(leaf.text.strip())
        else:
            parts.append(local(leaf.tag))
    return ", ".join(parts)


def collection_kind(resourcetype: ET.Element | None) -> str | None:
    if resourcetype is None:
        return None
    types = {local(c.tag) for c in resourcetype}
    if "collection" not in types:
        return None
    for kind in ("calendar", "schedule-inbox", "schedule-outbox", "addressbook"):
        if kind in types:
            return kind
    return "collection"


def human_size(nbytes: str) -> str:
    try:
        n = float(nbytes)
    except ValueError:
        return nbytes
    for unit in ("B", "KB", "MB"):
        if n < 1024 or unit == "MB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return nbytes


def props_of(propstat: ET.Element) -> list[ET.Element]:
    prop = propstat.find("D:prop", NS)
    return list(prop) if prop is not None else []


def pretty_xml(raw: str) -> str:
    try:
        text = xml.dom.minidom.parseString(raw.encode()).toprettyxml(indent="  ")
        return "\n".join(line for line in text.splitlines() if line.strip())
    except Exception:
        return raw


@dataclass
class Node:
    url: str
    kind: str
    name: str
    loaded: bool = False


@dataclass
class Child:
    url: str
    name: str
    kind: str
    content_type: str
    size: str


class Dav:
    def __init__(self, url: str, username: str, password: str):
        self.url, self.username, self.password = url, username, password
        self.client = self.make_client(url)

    def make_client(self, url: str) -> caldav.DAVClient:
        return caldav.DAVClient(url=url, username=self.username, password=self.password)

    def connect(self) -> tuple[str, str]:
        try:
            principal = self.client.principal()
        except caldav.lib.error.NotFoundError:
            self.client = self.make_client(urljoin(self.url, "/.well-known/caldav"))
            principal = self.client.principal()
        home = principal.calendar_home_set
        return str(principal.url), str(home.url)

    def propfind(self, url: str, body: str, depth: int) -> str:
        resp = self.client.request(
            url,
            "PROPFIND",
            body,
            {"Depth": str(depth), "Content-Type": 'application/xml; charset="utf-8"'},
        )
        if resp.status not in (200, 207):
            raise RuntimeError(
                f"PROPFIND {url} failed: HTTP {resp.status} {resp.reason}"
            )
        raw = resp.raw
        return raw.decode() if isinstance(raw, bytes) else raw

    def list_children(self, url: str) -> list[Child]:
        raw = self.propfind(url, LIST_BODY, depth=1)
        root = ET.fromstring(raw.encode())
        self_path = strip_slash(unquote(urlparse(url).path))
        children = []
        for resp in root.findall("D:response", NS):
            href = resp.findtext("D:href", default="", namespaces=NS).strip()
            full = urljoin(url, href)
            path = unquote(urlparse(full).path)
            if strip_slash(path) == self_path:
                continue
            props = {}
            for propstat in resp.findall("D:propstat", NS):
                status = propstat.findtext("D:status", default="", namespaces=NS)
                if " 200 " not in status:
                    continue
                for p in props_of(propstat):
                    props[local(p.tag)] = p
            kind = collection_kind(props.get("resourcetype")) or "file"
            fallback = strip_slash(path).rsplit("/", 1)[-1]
            name = fallback
            if kind != "file" and "displayname" in props:
                name = prop_value(props["displayname"]) or fallback
            ctype = (
                prop_value(props["getcontenttype"]) if "getcontenttype" in props else ""
            )
            size = (
                human_size(prop_value(props["getcontentlength"]))
                if kind == "file" and "getcontentlength" in props
                else ""
            )
            children.append(Child(full, name, kind, ctype.split(";")[0], size))
        children.sort(key=lambda c: (c.kind == "file", c.name.lower()))
        return children

    def describe(self, url: str, kind: str) -> str:
        body = PRINCIPAL_BODY if kind == "principal" else COLLECTION_BODY
        raw = self.propfind(url, body, depth=0)
        root = ET.fromstring(raw.encode())
        found, missing = [], []
        for propstat in root.iter("{DAV:}propstat"):
            status = propstat.findtext("D:status", default="", namespaces=NS)
            for p in props_of(propstat):
                if " 200 " in status:
                    found.append((local(p.tag), prop_value(p)))
                else:
                    missing.append(local(p.tag))

        out = [f"URL:  {url}", f"Type: {kind}", ""]
        if kind in TYPE_NOTES:
            out += [textwrap.fill(TYPE_NOTES[kind], 78), ""]
        out.append("Properties")
        out.append("----------")
        width = max((len(n) for n, _ in found), default=0)
        for name, value in found:
            out.append(f"{name.ljust(width)}  {value or '(empty)'}")
        if missing:
            out += ["", "Not provided by this server: " + ", ".join(missing)]
        out += [
            "",
            "",
            "Raw request (PROPFIND, Depth: 0)",
            "--------------------------------",
            body,
            "",
            "Raw response",
            "------------",
            pretty_xml(raw),
        ]
        return "\n".join(out)

    def get_file(self, url: str) -> str:
        resp = self.client.request(url, "GET")
        if resp.status != 200:
            raise RuntimeError(f"GET {url} failed: HTTP {resp.status} {resp.reason}")
        raw = resp.raw
        body = raw.decode(errors="replace") if isinstance(raw, bytes) else raw
        headers = resp.headers or {}
        out = [f"URL:            {url}"]
        for h in ("Content-Type", "ETag", "Last-Modified", "Content-Length"):
            if headers.get(h):
                out.append(f"{(h + ':').ljust(16)}{headers[h]}")
        out += ["", body.replace("\r\n", "\n")]
        return "\n".join(out)


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.dav: Dav | None = None
        self.nodes: dict[str, Node] = {}
        self.results: queue.Queue = queue.Queue()
        self.view_seq = 0
        self.shown_iid: str | None = None

        root.title("CalDAV Explorer")
        style = ttk.Style(root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        root.configure(background=style.lookup("TFrame", "background"))
        px = root.winfo_pixels
        root.geometry(f"{px('825p')}x{px('525p')}")
        root.minsize(px("450p"), px("300p"))
        self.mono = tkfont.nametofont("TkFixedFont")

        self.build_login()
        self.root.after(50, self.poll_results)

    def run_bg(self, fn, on_ok, on_err=None):
        def worker():
            try:
                result = fn()
            except Exception as e:
                detail = "".join(traceback.format_exception_only(type(e), e)).strip()
                self.results.put((on_err or self.show_error, detail))
            else:
                self.results.put((on_ok, result))

        threading.Thread(target=worker, daemon=True).start()

    def poll_results(self):
        try:
            while True:
                callback, value = self.results.get_nowait()
                callback(value)
        except queue.Empty:
            pass
        self.root.after(50, self.poll_results)

    def build_login(self):
        self.login = ttk.Frame(self.root, padding="18p")
        self.login.place(relx=0.5, rely=0.4, anchor="center")

        heading_font = tkfont.nametofont("TkDefaultFont").copy()
        heading_font.configure(size=14, weight="bold")
        ttk.Label(
            self.login, text="Connect to a CalDAV server", font=heading_font
        ).grid(row=0, column=0, columnspan=2, pady=(0, "12p"), sticky="w")
        self.url_var = tk.StringVar()
        self.user_var = tk.StringVar()
        self.pass_var = tk.StringVar()
        fields = [
            ("Server URL", self.url_var, {}),
            ("Username", self.user_var, {}),
            ("App password", self.pass_var, {"show": "•"}),
        ]
        for i, (label, var, opts) in enumerate(fields, start=1):
            ttk.Label(self.login, text=label).grid(
                row=i, column=0, sticky="w", pady="3p", padx=(0, "9p")
            )
            entry = ttk.Entry(self.login, textvariable=var, width=48, **opts)
            entry.grid(row=i, column=1, sticky="ew", pady="3p")
            entry.bind("<Return>", lambda _e: self.do_connect())
            if i == 1:
                entry.focus_set()

        ttk.Label(
            self.login,
            text="e.g. https://caldav.fastmail.com or https://caldav.icloud.com",
            foreground="gray",
        ).grid(row=4, column=1, sticky="w")

        self.connect_btn = ttk.Button(
            self.login, text="Connect", command=self.do_connect
        )
        self.connect_btn.grid(row=5, column=1, sticky="e", pady=("12p", 0))
        self.login_status = ttk.Label(
            self.login, text="", foreground="firebrick", wraplength="315p"
        )
        self.login_status.grid(
            row=6, column=0, columnspan=2, sticky="w", pady=("9p", 0)
        )

    def do_connect(self):
        url, user, pw = (
            self.url_var.get().strip(),
            self.user_var.get().strip(),
            self.pass_var.get(),
        )
        if not url:
            self.login_status.config(text="Enter a server URL.")
            return
        if "://" not in url:
            url = "https://" + url
        self.connect_btn.state(["disabled"])
        self.login_status.config(text="Connecting…", foreground="gray")

        def work():
            dav = Dav(url, user, pw)
            return dav, *dav.connect()

        self.run_bg(work, self.on_connected, self.on_connect_failed)

    def on_connect_failed(self, detail: str):
        self.connect_btn.state(["!disabled"])
        self.login_status.config(
            text=f"Couldn't connect:\n{detail.splitlines()[0]}",
            foreground="firebrick",
        )

    def on_connected(self, result):
        self.dav, principal_url, home_url = result
        self.pass_var.set("")
        self.login.destroy()
        self.build_explorer()
        host = urlparse(home_url).hostname or ""
        self.root.title(f"CalDAV Explorer: {self.user_var.get()} @ {host}")

        p = self.add_node(
            "", Node(principal_url, "principal", "Principal (you)", loaded=True)
        )
        self.add_node(p, Node(home_url, "collection", "Calendar home"))
        self.tree.item(p, open=True)
        self.tree.selection_set(p)

    def build_explorer(self):
        px = self.root.winfo_pixels
        outer = ttk.Frame(self.root)
        outer.pack(fill="both", expand=True)

        panes = ttk.PanedWindow(outer, orient="horizontal")
        panes.pack(fill="both", expand=True)

        left = ttk.Frame(panes)
        self.tree = ttk.Treeview(left, columns=("type", "size"), selectmode="browse")
        self.tree.heading("#0", text="Name", anchor="w")
        self.tree.heading("type", text="Type", anchor="w")
        self.tree.heading("size", text="Size", anchor="e")
        self.tree.column("#0", width=px("195p"), stretch=True)
        self.tree.column("type", width=px("90p"), stretch=False)
        self.tree.column("size", width=px("55p"), stretch=False, anchor="e")
        tree_scroll = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        tree_scroll.pack(side="right", fill="y")
        panes.add(left, weight=1)

        right = ttk.Frame(panes)
        self.text = tk.Text(
            right,
            wrap="none",
            font=self.mono,
            padx="6p",
            pady="6p",
            undo=False,
        )
        y_scroll = ttk.Scrollbar(right, orient="vertical", command=self.text.yview)
        x_scroll = ttk.Scrollbar(right, orient="horizontal", command=self.text.xview)
        self.text.configure(
            yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set, state="disabled"
        )
        right.rowconfigure(0, weight=1)
        right.columnconfigure(0, weight=1)
        self.text.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")
        panes.add(right, weight=3)

        self.status = ttk.Label(outer, text="", anchor="w", padding=("6p", "2p"))
        self.status.pack(fill="x", side="bottom")

        self.tree.bind("<<TreeviewOpen>>", self.on_open)
        self.tree.bind("<<TreeviewSelect>>", self.on_select)

    def add_node(self, parent: str, node: Node, ctype: str = "", size: str = "") -> str:
        type_label = (ctype or "file") if node.kind == "file" else node.kind
        iid = self.tree.insert(parent, "end", text=node.name, values=(type_label, size))
        self.nodes[iid] = node
        if node.kind not in ("principal", "file") and not node.loaded:
            self.tree.insert(iid, "end", text="Loading…")
        return iid

    def on_open(self, _event=None):
        self.root.after_idle(self.load_open_nodes)

    def load_open_nodes(self):
        for iid, node in list(self.nodes.items()):
            if (
                not node.loaded
                and node.kind not in ("principal", "file")
                and self.tree.exists(iid)
                and self.tree.item(iid, "open")
            ):
                self.load_children(iid, node)

    def load_children(self, iid: str, node: Node):
        node.loaded = True
        self.set_status(f"Listing {node.url} …")

        def done(children: list[Child]):
            self.tree.delete(*self.tree.get_children(iid))
            for c in children:
                self.add_node(iid, Node(c.url, c.kind, c.name), c.content_type, c.size)
            if not children:
                self.tree.insert(iid, "end", text="(empty)")
            self.set_status(f"{len(children)} item(s) in {node.name}")

        def failed(detail: str):
            node.loaded = False
            self.tree.delete(*self.tree.get_children(iid))
            first_line = detail.splitlines()[0]
            self.tree.insert(iid, "end", text=f"Couldn't load: {first_line}")
            self.set_status(f"Couldn't list {node.name}: {first_line}")

        self.run_bg(lambda: self.dav.list_children(node.url), done, failed)

    def on_select(self, _event=None):
        sel = self.tree.selection()
        node = self.nodes.get(sel[0]) if sel else None
        if not node or sel[0] == self.shown_iid:
            return
        self.shown_iid = sel[0]
        self.view_seq += 1
        seq = self.view_seq
        self.show_text(f"Loading {node.url} …")
        self.set_status(f"Fetching {node.name} …")

        def fn():
            if node.kind == "file":
                return self.dav.get_file(node.url)
            return self.dav.describe(node.url, node.kind)

        def done(text: str):
            if seq == self.view_seq:
                self.show_text(text)
                self.set_status(node.url)

        def failed(detail: str):
            if seq == self.view_seq:
                self.show_text(f"Error loading {node.url}\n\n{detail}")
                self.shown_iid = None
                self.set_status("Error")

        self.run_bg(fn, done, failed)

    def show_text(self, content: str):
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", content)
        self.text.configure(state="disabled")
        self.text.yview_moveto(0)
        self.text.xview_moveto(0)

    def show_error(self, detail: str):
        self.set_status(f"Error: {detail}")

    def set_status(self, msg: str):
        if hasattr(self, "status"):
            self.status.config(text=msg)


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
