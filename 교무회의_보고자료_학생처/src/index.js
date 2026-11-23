// 교무회의 보고자료(학생처) 검토기 — 공용 저장소 (Durable Object 1개에 현재 문서 1건 저장)
const MAX_ITEMS = 3000, MAX_TEXT = 3000, CHUNK = 100 * 1024, MAX_FILE = 12 * 1024 * 1024;

export class Store {
  constructor(state) {
    this.state = state;
    this.doc = null;
    state.blockConcurrencyWhile(async () => {
      this.doc = (await state.storage.get('doc')) || null;
    });
  }
  async save() { await this.state.storage.put('doc', this.doc); }
  async clearFile() {
    const n = (this.doc && this.doc.fileChunks) || 0;
    const keys = [];
    for (let i = 0; i < n; i++) keys.push('f' + i);
    for (let i = 0; i < keys.length; i += 100) await this.state.storage.delete(keys.slice(i, i + 100));
  }
  async fetch(req) {
    const url = new URL(req.url);
    const out = (o, s = 200) => new Response(JSON.stringify(o), {
      status: s,
      headers: { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' },
    });
    const m = req.method;
    if (url.pathname === '/api/file') {
      const id = url.searchParams.get('doc');
      if (!this.doc || this.doc.docId !== id) return out({ error: 'doc' }, 409);
      if (m === 'PUT') {
        const buf = await req.arrayBuffer();
        if (!buf.byteLength || buf.byteLength > MAX_FILE) return out({ error: 'size' }, 413);
        await this.clearFile();
        const n = Math.ceil(buf.byteLength / CHUNK);
        for (let i = 0; i < n; i += 100) {
          const obj = {};
          for (let j = i; j < Math.min(n, i + 100); j++) obj['f' + j] = buf.slice(j * CHUNK, (j + 1) * CHUNK);
          await this.state.storage.put(obj);
        }
        this.doc.hasFile = true; this.doc.fileChunks = n; this.doc.rev += 1; this.doc.updatedAt = Date.now();
        await this.save();
        return out({ doc: this.doc });
      }
      if (m === 'GET') {
        const n = this.doc.fileChunks || 0;
        if (!this.doc.hasFile || !n) return out({ error: 'nofile' }, 404);
        const parts = [];
        for (let i = 0; i < n; i += 100) {
          const keys = [];
          for (let j = i; j < Math.min(n, i + 100); j++) keys.push('f' + j);
          const got = await this.state.storage.get(keys);
          for (const k of keys) parts.push(new Uint8Array(got.get(k)));
        }
        const total = parts.reduce((a, p) => a + p.length, 0), all = new Uint8Array(total);
        let o = 0; for (const p of parts) { all.set(p, o); o += p.length; }
        return new Response(all, { headers: { 'content-type': 'application/octet-stream', 'cache-control': 'no-store' } });
      }
      return out({ error: 'method' }, 405);
    }
    if (url.pathname === '/api/settings') {
      if (m === 'GET') return out({ settings: (await this.state.storage.get('settings')) || null });
      if (m === 'PUT') {
        let b = {}; try { b = await req.json(); } catch (e) {}
        const st = { doneRange: String(b.doneRange ?? '').slice(0, 100), planRange: String(b.planRange ?? '').slice(0, 100), deadline: /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(String(b.deadline ?? '')) ? String(b.deadline) : '' };
        await this.state.storage.put('settings', st);
        return out({ settings: st });
      }
      return out({ error: 'method' }, 405);
    }
    if (url.pathname === '/api/template') {
      const meta = await this.state.storage.get('tplmeta');
      if (m === 'GET') {
        if (url.searchParams.get('meta')) return out({ meta: meta || null });
        if (!meta) return out({ error: 'none' }, 404);
        const parts = [];
        for (let i = 0; i < meta.chunks; i += 100) {
          const keys = [];
          for (let j = i; j < Math.min(meta.chunks, i + 100); j++) keys.push('t' + j);
          const got = await this.state.storage.get(keys);
          for (const k of keys) parts.push(new Uint8Array(got.get(k)));
        }
        const total = parts.reduce((a, p) => a + p.length, 0), all = new Uint8Array(total);
        let o = 0; for (const p of parts) { all.set(p, o); o += p.length; }
        return new Response(all, { headers: { 'content-type': 'application/octet-stream', 'cache-control': 'no-store' } });
      }
      const clearT = async () => {
        if (!meta) return;
        const keys = []; for (let i = 0; i < meta.chunks; i++) keys.push('t' + i);
        for (let i = 0; i < keys.length; i += 100) await this.state.storage.delete(keys.slice(i, i + 100));
        await this.state.storage.delete('tplmeta');
      };
      if (m === 'PUT') {
        const buf = await req.arrayBuffer();
        if (!buf.byteLength || buf.byteLength > MAX_FILE) return out({ error: 'size' }, 413);
        await clearT();
        const n = Math.ceil(buf.byteLength / CHUNK);
        for (let i = 0; i < n; i += 100) {
          const obj = {};
          for (let j = i; j < Math.min(n, i + 100); j++) obj['t' + j] = buf.slice(j * CHUNK, (j + 1) * CHUNK);
          await this.state.storage.put(obj);
        }
        const nm = { name: decodeURIComponent(url.searchParams.get('name') || '').slice(0, 200), at: Date.now(), chunks: n, size: buf.byteLength };
        await this.state.storage.put('tplmeta', nm);
        return out({ meta: nm });
      }
      if (m === 'DELETE') { await clearT(); return out({ meta: null }); }
      return out({ error: 'method' }, 405);
    }
    if (url.pathname === '/api/dept') {
      const n = Number(url.searchParams.get('n'));
      if (!n) {
        const depts = [];
        for (let k = 1; k <= 6; k++) {
          const d = await this.state.storage.get('dept' + k);
          const t = (await this.state.storage.get('deptText' + k)) || [];
          const ta = (await this.state.storage.get('deptTextAt' + k)) || 0;
          depts.push(d || t.length ? { n: k, fileName: d ? d.fileName : '', at: d ? d.at : 0, count: d ? d.lines.length : 0, textCount: t.length, textAt: ta } : null);
        }
        return out({ depts });
      }
      if (!Number.isInteger(n) || n < 1 || n > 6) return out({ error: 'dept' }, 400);
      if (url.searchParams.get('kind') === 'text') {
        if (m === 'GET') return out({ items: (await this.state.storage.get('deptText' + n)) || [] });
        if (m === 'PUT') {
          let b = {}; try { b = await req.json(); } catch (e) {}
          const items = (Array.isArray(b.items) ? b.items : []).slice(0, 200)
            .map(x => ({ s: x && x.s === 'B' ? 'B' : 'A', t: String((x && x.t) ?? '').replace(/[\r\n]+/g, ' ').trim().slice(0, 500) }))
            .filter(x => x.t);
          if (items.length) { await this.state.storage.put('deptText' + n, items); await this.state.storage.put('deptTextAt' + n, Date.now()); } else await this.state.storage.delete(['deptText' + n, 'deptTextAt' + n]);
          return out({ ok: true, count: items.length });
        }
        return out({ error: 'method' }, 405);
      }
      if (m === 'GET') {
        const d = await this.state.storage.get('dept' + n);
        return d ? out(d) : out({ error: 'none' }, 404);
      }
      if (m === 'PUT') {
        let b = {}; try { b = await req.json(); } catch (e) {}
        const lines = Array.isArray(b.lines) ? b.lines.map(x => String(x).slice(0, 1000)) : [];
        if (!lines.length || lines.length > 600 || lines.join('').length > 90000) return out({ error: 'size' }, 413);
        const d = { fileName: String(b.fileName ?? '').slice(0, 200), at: Date.now(), lines };
        await this.state.storage.put('dept' + n, d);
        return out({ ok: true, at: d.at });
      }
      if (m === 'DELETE') { await this.state.storage.delete(['dept' + n, 'deptText' + n, 'deptTextAt' + n]); return out({ ok: true }); }
      return out({ error: 'method' }, 405);
    }
    if (m === 'GET') {
      const rev = Number(url.searchParams.get('rev')), id = url.searchParams.get('doc');
      if (this.doc && rev === this.doc.rev && id === this.doc.docId) return out({ unchanged: true });
      return out({ doc: this.doc });
    }
    let body = {};
    try { body = await req.json(); } catch (e) {}
    if (m === 'POST') {
      const items = body.items;
      if (!Array.isArray(items) || !items.length || items.length > MAX_ITEMS) return out({ error: 'bad items' }, 400);
      const clean = items.map(i => ({
        text: String(i.text ?? '').slice(0, MAX_TEXT),
        fixed: !!i.fixed, del: false, star: false,
        sec: Number.isFinite(i.sec) ? i.sec : undefined, ph: Number.isFinite(i.ph) ? i.ph : undefined,
        ln: Number.isFinite(i.ln) ? i.ln : undefined, nl: Number.isFinite(i.nl) ? i.nl : undefined,
        dept: Number.isInteger(i.dept) && i.dept >= 1 && i.dept <= 6 ? i.dept : undefined,
      }));
      await this.clearFile();
      this.doc = {
        docId: crypto.randomUUID(), rev: 1,
        fileName: String(body.fileName ?? '').slice(0, 200),
        stage: 'edit', items: clean, updatedAt: Date.now(),
      };
      await this.save();
      return out({ doc: this.doc });
    }
    if (m === 'PATCH') {
      if (!this.doc) return out({ doc: null });
      if (body.docId !== this.doc.docId) return out({ doc: this.doc, conflict: true });
      for (const op of Array.isArray(body.ops) ? body.ops : []) {
        const it = this.doc.items[op.i];
        if (!it || it.fixed) continue;
        it.del = !!op.del;
        it.star = it.del ? false : !!op.star;
      }
      if (body.stage === 'done' || body.stage === 'edit') this.doc.stage = body.stage;
      this.doc.rev += 1; this.doc.updatedAt = Date.now();
      await this.save();
      return out({ doc: this.doc });
    }
    if (m === 'DELETE') {
      await this.clearFile();
      this.doc = null;
      await this.state.storage.delete('doc');
      return out({ doc: null });
    }
    return out({ error: 'method' }, 405);
  }
}

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    if (url.pathname === '/api/state' || url.pathname === '/api/file' || url.pathname === '/api/dept' || url.pathname === '/api/settings' || url.pathname === '/api/template') {
      return env.STORE.get(env.STORE.idFromName('main')).fetch(req);
    }
    return env.ASSETS.fetch(req);
  },
};
