// 한글 보고서 검토기 — 공용 저장소 (Durable Object 1개에 현재 문서 1건 저장)
const MAX_ITEMS = 3000, MAX_TEXT = 3000;

export class Store {
  constructor(state) {
    this.state = state;
    this.doc = null;
    state.blockConcurrencyWhile(async () => {
      this.doc = (await state.storage.get('doc')) || null;
    });
  }
  async save() { await this.state.storage.put('doc', this.doc); }
  async fetch(req) {
    const url = new URL(req.url);
    const out = (o, s = 200) => new Response(JSON.stringify(o), {
      status: s,
      headers: { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' },
    });
    const m = req.method;
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
      }));
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
    if (url.pathname === '/api/state') {
      return env.STORE.get(env.STORE.idFromName('main')).fetch(req);
    }
    return env.ASSETS.fetch(req);
  },
};
