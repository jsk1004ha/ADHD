"""Local, provenance-first memory. Retrieved records are DATA, never instructions.

SQLite/FTS5 + Korean character bigrams + reciprocal rank fusion; no model/API/GPU.
Inspired by documented memory systems, not their
complete implementations. Exact-key conflicts are detectable; arbitrary semantic
contradictions are not. Raw conversations and secret-bearing records are opt-in.
"""
from __future__ import annotations
import hashlib
import json
import math
import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any
from .core import store

KINDS = {'fact','preference','decision','episode','procedure','incident','document','skill'}
BASIS = {'user_statement','source_read','measured','calculated','inferred','verified_run'}
LIFECYCLE = {'active','conflict','superseded','quarantined','needs_review','forgotten'}
SECRET = re.compile(r'(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{16,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY|(?:api[_-]?key|password|token)\s*[:=]\s*["\']?[^\s"\']{12,})', re.I)

def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False,sort_keys=True).encode()).hexdigest()

def tokens(text: str) -> set[str]:
    words = re.findall(r'[a-z0-9_]+|[가-힣]+', text.lower())
    return set(words) | {w[i:i+2] for w in words if re.search('[가-힣]',w) for i in range(len(w)-1)}

def namespace(workspace: Path) -> str:
    return 'project:'+fingerprint(str(workspace.expanduser().resolve()))[:24]


def validate_procedure_bundle(bundle: dict|None) -> dict|None:
    if bundle is None:
        return None
    required={'input_conditions','execution_script','verifier','recovery'}
    if not isinstance(bundle,dict) or set(bundle)!=required:
        raise ValueError('Procedure bundle needs input_conditions, execution_script, verifier and recovery')
    for field in required:
        values=bundle[field]
        if (not isinstance(values,list) or not 1<=len(values)<=10 or
                any(not isinstance(s,str) or not s.strip() or len(s)>1000 for s in values)):
            raise ValueError('Procedure bundle entries must be bounded nonempty text')
    return bundle

class Memory:
    def __init__(self, path: Path|None=None):
        self.path = path or store()/'memory-v2.sqlite3'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=8)
        self.db.row_factory=sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS records (
          id TEXT PRIMARY KEY, scope TEXT NOT NULL, kind TEXT NOT NULL,
          logical_key TEXT NOT NULL, content TEXT NOT NULL, source TEXT NOT NULL,
          locator TEXT NOT NULL, basis TEXT NOT NULL, observed REAL NOT NULL,
          expires REAL, status TEXT NOT NULL, digest TEXT NOT NULL,
          entities TEXT NOT NULL, environment TEXT NOT NULL, created REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS scoped_records ON records(scope,status,logical_key);
        CREATE TABLE IF NOT EXISTS tombstones(digest TEXT PRIMARY KEY, at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS edges(a TEXT,b TEXT,relation TEXT,
          PRIMARY KEY(a,b,relation), FOREIGN KEY(a) REFERENCES records(id) ON DELETE CASCADE,
          FOREIGN KEY(b) REFERENCES records(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, at REAL, op TEXT, record_id TEXT);
        CREATE TABLE IF NOT EXISTS record_aliases (
          namespace TEXT NOT NULL, alias TEXT NOT NULL, canonical_id TEXT NOT NULL,
          PRIMARY KEY(namespace,alias));
        CREATE INDEX IF NOT EXISTS aliases_by_canonical ON record_aliases(canonical_id);
        CREATE TABLE IF NOT EXISTS observations (
          observation_id TEXT PRIMARY KEY, canonical_id TEXT NOT NULL,
          observed_at REAL NOT NULL, source TEXT NOT NULL, locator TEXT NOT NULL,
          source_revision TEXT NOT NULL, evidence_digest TEXT NOT NULL,
          environment TEXT NOT NULL, expires_at REAL,
          UNIQUE(canonical_id,evidence_digest,source_revision));
        CREATE TABLE IF NOT EXISTS lifecycle (
          canonical_id TEXT PRIMARY KEY, scope TEXT NOT NULL,
          status TEXT NOT NULL, changed_at REAL NOT NULL, reason_ref TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS import_identities (
          scope TEXT NOT NULL, content_digest TEXT NOT NULL, canonical_id TEXT NOT NULL,
          PRIMARY KEY(scope,content_digest));
        CREATE TABLE IF NOT EXISTS import_source_aliases (
          scope TEXT NOT NULL, canonical_id TEXT NOT NULL, source_alias TEXT NOT NULL,
          PRIMARY KEY(scope,canonical_id,source_alias));
        CREATE TABLE IF NOT EXISTS procedure_goals (
          canonical_id TEXT NOT NULL, goal TEXT NOT NULL, successes INTEGER NOT NULL,
          updated REAL NOT NULL, PRIMARY KEY(canonical_id,goal));
        CREATE TABLE IF NOT EXISTS migrations (
          name TEXT PRIMARY KEY, completed_at REAL NOT NULL, detail TEXT NOT NULL);
        ''')
        try:
            self.db.execute('CREATE VIRTUAL TABLE IF NOT EXISTS recall_fts USING fts5(id UNINDEXED, text, tokenize="unicode61")')
            self.fts=True
        except sqlite3.OperationalError:
            self.fts=False
        self.db.commit()

    def _canonical(self, rid: str) -> str | None:
        if self.db.execute('SELECT 1 FROM records WHERE id=?',(rid,)).fetchone():
            return rid
        row=self.db.execute('SELECT canonical_id FROM record_aliases WHERE alias=? ORDER BY namespace LIMIT 1',(rid,)).fetchone()
        return row['canonical_id'] if row else None

    def add_alias(self, namespace_name: str, alias: str, canonical_id: str) -> None:
        old=self.db.execute('SELECT canonical_id FROM record_aliases WHERE alias=?',(alias,)).fetchone()
        if old and old['canonical_id']!=canonical_id:
            raise ValueError('Memory alias already maps to a different canonical record')
        self.db.execute('INSERT OR IGNORE INTO record_aliases VALUES (?,?,?)',(namespace_name,alias,canonical_id))

    def _lifecycle(self, canonical_id: str) -> str | None:
        row=self.db.execute('SELECT status FROM lifecycle WHERE canonical_id=?',(canonical_id,)).fetchone()
        return row['status'] if row else None

    def _set_lifecycle(self, canonical_id: str, scope: str, status: str, reason_ref: str) -> None:
        if status not in LIFECYCLE: raise ValueError('Unknown lifecycle state')
        self.db.execute('''INSERT INTO lifecycle VALUES (?,?,?,?,?)
            ON CONFLICT(canonical_id) DO UPDATE SET scope=excluded.scope,status=excluded.status,
            changed_at=excluded.changed_at,reason_ref=excluded.reason_ref''',
            (canonical_id,scope,status,time.time(),reason_ref[:500]))

    def close(self): self.db.close()
    def __enter__(self): return self
    def __exit__(self,*args): self.close()

    def put(self, *, scope: str, kind: str, key: str, content: str, source: str,
            locator: str, basis: str='source_read', observed: float|None=None,
            ttl_days: float|None=None, entities: list[str]|None=None,
            environment: str='', verified_receipt: str|None=None,
            content_identity: str|None=None, source_alias: str|None=None) -> dict:
        if kind not in KINDS or basis not in BASIS: raise ValueError('Unknown memory kind/basis')
        if scope!='global' and not re.fullmatch(r'project:[a-f0-9]{24}',scope):
            raise ValueError('Scope must be global or an exact workspace namespace')
        if not all(isinstance(x,str) and x.strip() for x in [key,content,source,locator]):
            raise ValueError('Memory requires key/content/source/locator')
        if max(len(content),len(source),len(locator))>6000 or len(key)>200:
            raise ValueError('Store a concise record and source pointer, not whole transcripts')
        if SECRET.search(content+' '+source+' '+locator):
            raise ValueError('Possible secret: record rejected, not logged')
        if kind=='procedure' and not verified_receipt:
            raise ValueError('Procedure promotion requires an independently verified completion receipt')
        if basis=='verified_run' and not verified_receipt:
            raise ValueError('Missing verified-run receipt')
        now=time.time(); observed=now if observed is None else float(observed)
        if not math.isfinite(observed) or observed>now+300: raise ValueError('Invalid/future observation time')
        if ttl_days is not None and not (0<float(ttl_days)<=3650): raise ValueError('TTL days must be >0..3650')
        entities=entities or []
        if len(entities)>16 or any(not isinstance(x,str) or len(x)>100 for x in entities):
            raise ValueError('At most 16 short entity labels')
        digest=fingerprint([scope,kind,key,content])
        content_digest=content_identity or fingerprint([kind,content])
        with self.db:
            canonical_id=None
            if content_identity is not None:
                identity=self.db.execute('SELECT canonical_id FROM import_identities WHERE scope=? AND content_digest=?',(scope,content_digest)).fetchone()
                canonical_id=identity['canonical_id'] if identity else fingerprint(['import',scope,content_digest])[:32]
                lifecycle=self._lifecycle(canonical_id)
                if lifecycle in {'forgotten','quarantined'}:
                    return {'status':lifecycle,'id':canonical_id}
            if self.db.execute('SELECT 1 FROM tombstones WHERE digest=?',(digest,)).fetchone():
                return {'status':'forgotten','id':None}
            old=self.db.execute('SELECT id,status FROM records WHERE digest=?',(digest,)).fetchone()
            if old:
                if source_alias:
                    self.db.execute('INSERT OR IGNORE INTO import_source_aliases VALUES (?,?,?)',(scope,old['id'],source_alias))
                return dict(old)
            if canonical_id:
                old=self.db.execute('SELECT id,status FROM records WHERE id=?',(canonical_id,)).fetchone()
                if old:
                    if source_alias:self.db.execute('INSERT OR IGNORE INTO import_source_aliases VALUES (?,?,?)',(scope,canonical_id,source_alias))
                    return dict(old)
            # Expired facts remain auditable but do not veto a new observation.
            self.db.execute("UPDATE records SET status='superseded' WHERE scope=? AND logical_key=? AND expires IS NOT NULL AND expires<=?",(scope,key,now))
            conflict=self.db.execute("SELECT id FROM records WHERE scope=? AND logical_key=? AND status IN ('active','conflict')",(scope,key)).fetchone()
            status='conflict' if conflict else 'active'
            if conflict:
                self.db.execute("UPDATE records SET status='conflict' WHERE scope=? AND logical_key=? AND status='active'",(scope,key))
                for prior in self.db.execute("SELECT id FROM records WHERE scope=? AND logical_key=? AND status='conflict'",(scope,key)):
                    self._set_lifecycle(prior['id'],scope,'conflict','logical-key-conflict')
            rid=canonical_id or uuid.uuid4().hex
            self.db.execute('INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (rid,scope,kind,key,content,source,locator,basis,observed,
                 observed+float(ttl_days)*86400 if ttl_days is not None else None,status,digest,
                 json.dumps(entities,ensure_ascii=False),environment,now))
            self._set_lifecycle(rid,scope,status,'retain')
            if content_identity is not None:
                self.db.execute('INSERT OR IGNORE INTO import_identities VALUES (?,?,?)',(scope,content_digest,rid))
                self.db.execute('INSERT OR IGNORE INTO import_source_aliases VALUES (?,?,?)',(scope,rid,source_alias or source))
            if self.fts:
                self.db.execute('INSERT INTO recall_fts VALUES (?,?)',(rid,' '.join(sorted(tokens(key+' '+content)))))
            # Explicit entity overlap: one-hop pointers only, no generated factual relations.
            if entities:
                for row in self.db.execute("SELECT id,entities FROM records WHERE scope=? AND status='active' AND id!=? ORDER BY created DESC LIMIT 500",(scope,rid)):
                    if set(entities)&set(json.loads(row['entities'])):
                        self.db.execute('INSERT OR IGNORE INTO edges VALUES (?,?,?)',(rid,row['id'],'shares_entity'))
            self.db.execute('INSERT INTO events(at,op,record_id) VALUES (?,?,?)',(now,'retain:'+status,rid))
        return {'id':rid,'status':status}

    def observe(self, *, observation_id: str, evidence_digest: str, source_revision: str,
                evidence_ref: str='', **record: Any) -> dict:
        """Record a verified observation. Replayed evidence never renews expiry."""
        if not all(isinstance(x,str) and x.strip() for x in (observation_id,evidence_digest,source_revision)):
            raise ValueError('Observation identity, evidence digest and source revision are required')
        observed=float(record.get('observed',time.time()))
        ttl_days=record.get('ttl_days')
        result=self.put(**record)
        rid=result.get('id')
        if not rid or result.get('status') in {'forgotten','quarantined'}:
            return {**result,'observation':'blocked'}
        expires=observed+float(ttl_days)*86400 if ttl_days is not None else None
        with self.db:
            reused=self.db.execute('SELECT canonical_id,evidence_digest,source_revision FROM observations WHERE observation_id=?',
                                   (observation_id,)).fetchone()
            if reused and (reused['canonical_id'],reused['evidence_digest'],reused['source_revision'])!=(rid,evidence_digest,source_revision):
                raise ValueError('Observation id was already used for different evidence')
            duplicate=reused or self.db.execute('''SELECT observation_id FROM observations
                WHERE canonical_id=? AND evidence_digest=? AND source_revision=?''',
                (rid,evidence_digest,source_revision)).fetchone()
            if duplicate:
                return {**result,'observation':'duplicate','renewed':False}
            self.db.execute('INSERT INTO observations VALUES (?,?,?,?,?,?,?,?,?)',
                (observation_id,rid,observed,record['source'],record['locator'],source_revision,
                 evidence_digest,record.get('environment',''),expires))
            if expires is not None:
                self.db.execute('UPDATE records SET observed=MAX(observed,?),expires=MAX(COALESCE(expires,0),?) WHERE id=?',
                                (observed,expires,rid))
            self.db.execute('INSERT INTO events(at,op,record_id) VALUES (?,?,?)',
                            (time.time(),'observe:'+fingerprint(evidence_ref or evidence_digest),rid))
        return {**result,'observation':'recorded','renewed':ttl_days is not None}

    def resolve(self, rid: str, *, evidence: str) -> dict:
        """Explicit resolution only; caller must inspect current user/source evidence."""
        if not evidence.strip() or len(evidence)>2000: raise ValueError('Resolution needs current evidence')
        canonical=self._canonical(rid)
        if canonical and self._lifecycle(canonical) in {'forgotten','quarantined'}:
            raise ValueError('Forgotten or quarantined memory requires explicit restore')
        row=self.db.execute('SELECT * FROM records WHERE id=?',(canonical or rid,)).fetchone()
        if not row: raise ValueError('Unknown memory record')
        with self.db:
            other_ids=[r['id'] for r in self.db.execute('SELECT id FROM records WHERE scope=? AND logical_key=? AND id!=?',
                                                        (row['scope'],row['logical_key'],row['id']))]
            self.db.execute("UPDATE records SET status='superseded' WHERE scope=? AND logical_key=? AND id!=?",(row['scope'],row['logical_key'],row['id']))
            for other_id in other_ids:self._set_lifecycle(other_id,row['scope'],'superseded','resolved-by:'+row['id'])
            self.db.execute("UPDATE records SET status='active' WHERE id=?",(row['id'],))
            self._set_lifecycle(row['id'],row['scope'],'active','resolve:'+fingerprint(evidence))
            # No raw evidence/log content is re-injected into prompts.
            self.db.execute('INSERT INTO events(at,op,record_id) VALUES (?,?,?)',(time.time(),'resolve:'+fingerprint(evidence),rid))
        return {'id':row['id'],'status':'active','limitation':'Evidence existence checked; semantic truth requires review'}

    def forget(self,rid: str) -> dict:
        canonical=self._canonical(rid)
        row=self.db.execute('SELECT id,scope,digest FROM records WHERE id=?',(canonical or rid,)).fetchone()
        if not row: raise ValueError('Unknown memory record')
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO tombstones VALUES (?,?)',(row['digest'],time.time()))
            self._set_lifecycle(row['id'],row['scope'],'forgotten','explicit-forget')
            self.db.execute('DELETE FROM records WHERE id=?',(row['id'],))
            if self.fts: self.db.execute('DELETE FROM recall_fts WHERE id=?',(row['id'],))
            self.db.execute('INSERT INTO events(at,op,record_id) VALUES (?,?,?)',(time.time(),'forget',row['id']))
        return {'forgotten':row['id'],'note':'Removed from this active store; original files and external backups are not erased'}

    def quarantine(self, ids: list[str], *, reason: str='failed replay') -> list[str]:
        changed=[]
        with self.db:
            for alias in ids:
                rid=self._canonical(alias)
                if not rid: continue
                row=self.db.execute('SELECT scope FROM records WHERE id=?',(rid,)).fetchone()
                if not row: continue
                self.db.execute("UPDATE records SET status='quarantined' WHERE id=?",(rid,))
                self._set_lifecycle(rid,row['scope'],'quarantined',reason)
                self.db.execute('INSERT INTO events(at,op,record_id) VALUES (?,?,?)',(time.time(),'quarantine',rid))
                changed.append(rid)
        return changed

    def restore(self, rid: str, *, evidence: str, verified_receipt: str|None=None) -> dict:
        canonical=self._canonical(rid)
        if not canonical: raise ValueError('Forgotten records have no retained body; re-observe them with current evidence')
        row=self.db.execute('SELECT scope,kind FROM records WHERE id=?',(canonical,)).fetchone()
        if not row: raise ValueError('Unknown memory record')
        if not evidence.strip(): raise ValueError('Restore requires current evidence')
        if row['kind']=='procedure' and not verified_receipt:
            raise ValueError('Procedure restore requires a verified completion receipt')
        with self.db:
            self.db.execute("UPDATE records SET status='active' WHERE id=?",(canonical,))
            self._set_lifecycle(canonical,row['scope'],'active','restore:'+fingerprint(evidence))
        return {'id':canonical,'status':'active'}

    def save_procedure(self, workspace: Path, mode: str, goal: str, steps: list[str],
                       evidence: str, environment: str, *, legacy_alias: str|None=None,
                       bundle: dict|None=None) -> str:
        validate_procedure_bundle(bundle)
        scope=namespace(workspace)
        identity=fingerprint([scope,mode,steps,environment,bundle])
        key='procedure:'+mode+':'+identity[:24]
        content=json.dumps({'mode':mode,'steps':steps,'bundle':bundle},ensure_ascii=False,sort_keys=True)
        result=self.put(scope=scope,kind='procedure',key=key,content=content,
            source='verified-completion',locator=evidence[:6000] or 'completion receipt',
            basis='verified_run',environment=environment,verified_receipt=evidence or 'legacy receipt',
            content_identity='procedure:'+identity,source_alias='procedure:'+mode)
        rid=result.get('id')
        if rid and result.get('status')=='quarantined':
            self.restore(rid,evidence=evidence or 'new verified completion',verified_receipt=evidence or 'legacy receipt')
        elif not rid or result.get('status')=='forgotten':
            raise ValueError('Procedure is not eligible for active storage')
        with self.db:
            self.db.execute('''INSERT INTO procedure_goals VALUES (?,?,1,?)
                ON CONFLICT(canonical_id,goal) DO UPDATE SET successes=procedure_goals.successes+1,updated=excluded.updated''',
                (rid,goal,time.time()))
            if legacy_alias:self.add_alias('legacy-recipe',legacy_alias,rid)
        return rid

    def procedure_recipes(self, workspace: Path, mode: str, goal: str, environment: str,
                          limit: int=3) -> list[dict[str,Any]]:
        q=tokens(goal);scope=namespace(workspace);ranked=[]
        rows=self.db.execute('''SELECT r.id,r.content,g.goal,g.successes FROM records r
          JOIN procedure_goals g ON g.canonical_id=r.id
          LEFT JOIN lifecycle l ON l.canonical_id=r.id
          WHERE r.scope=? AND r.kind='procedure' AND r.environment=? AND r.status='active'
          AND COALESCE(l.status,'active')='active' ''',(scope,environment)).fetchall()
        for row in rows:
            previous=row['goal'];overlap=len(q&tokens(previous))/max(1,len(q|tokens(previous)))
            if overlap>=.22 or previous==goal:
                payload=json.loads(row['content'])
                ranked.append({'id':row['id'],'goal':previous,'steps':payload['steps'],
                    'bundle':payload.get('bundle'),'verified_runs':row['successes'],'similarity':overlap})
        return sorted(ranked,key=lambda d:(-d['similarity'],-d['verified_runs']))[:limit]

    def migrate_legacy_recipes(self, legacy_path: Path, *, backup_path: Path|None=None) -> dict:
        legacy_path=legacy_path.resolve()
        if not legacy_path.is_file(): return {'status':'absent','migrated':0}
        marker='legacy-recipes:'+fingerprint(str(legacy_path))
        if self.db.execute('SELECT 1 FROM migrations WHERE name=?',(marker,)).fetchone():
            return {'status':'already_migrated','migrated':0}
        backup_path=backup_path or legacy_path.with_name(legacy_path.name+'.pre-v011.bak')
        if not backup_path.exists():
            source=sqlite3.connect(legacy_path);dest=sqlite3.connect(backup_path)
            try: source.backup(dest)
            finally: dest.close();source.close()
        legacy=sqlite3.connect(legacy_path);legacy.row_factory=sqlite3.Row;migrated=0
        try:
            rows=legacy.execute('SELECT * FROM recipes ORDER BY id').fetchall()
            for row in rows:
                if self._canonical(row['id']):
                    continue
                steps=json.loads(row['steps'])
                rid=self.save_procedure(Path(row['workspace']),row['mode'],row['goal'],steps,
                    row['evidence'],row['environment'],legacy_alias=row['id'])
                with self.db:
                    self.db.execute('UPDATE procedure_goals SET successes=MAX(successes,?) WHERE canonical_id=? AND goal=?',
                                    (max(1,row['successes']),rid,row['goal']))
                if row['failures']:
                    self.quarantine([rid],reason='legacy failures='+str(row['failures']))
                migrated+=1
            with self.db:
                self.db.execute('INSERT INTO migrations VALUES (?,?,?)',(marker,time.time(),str(legacy_path)))
                self.db.execute('PRAGMA user_version=3')
        finally: legacy.close()
        return {'status':'migrated','migrated':migrated,'backup':str(backup_path)}

    def recall(self,query: str,scope: str,*,limit:int=6,max_chars:int=6500,
               environment: str='',include_global:bool=True,now:float|None=None) -> dict:
        if not 1<=limit<=12 or not 500<=max_chars<=16000: raise ValueError('Invalid recall budget')
        now=time.time() if now is None else now; q=tokens(query)
        scopes=[scope]+(['global'] if include_global and scope!='global' else [])
        sql='SELECT * FROM records WHERE scope IN ('+','.join('?' for _ in scopes)+") AND status='active' AND (expires IS NULL OR expires>?)"
        all_rows=self.db.execute(sql,(*scopes,now)).fetchall()
        rows={r['id']:dict(r) for r in all_rows if not r['environment'] or r['environment']==environment}
        # Filters happen BEFORE ranking; a wrong-project match cannot displace a valid record.
        ranks={}
        lex=sorted(((len(q&tokens(r['logical_key']+' '+r['content']))/math.sqrt(max(1,len(tokens(r['content'])))),i) for i,r in rows.items()),reverse=True)
        lex=[(score,i) for score,i in lex if score>0]
        for n,(_,i) in enumerate(lex): ranks[i]=1/(60+n)
        if self.fts and q:
            expression=' OR '.join('"'+t.replace('"','""')+'"' for t in sorted(q)[:40])
            hits=self.db.execute('SELECT id FROM recall_fts WHERE recall_fts MATCH ? ORDER BY bm25(recall_fts)',(expression,))
            n=0
            for h in hits:
                if h['id'] in rows:
                    ranks[h['id']]=ranks.get(h['id'],0)+1/(60+n);n+=1
                    if n>=200:break
        selected=[];size=0
        for rid in sorted(ranks,key=lambda i:(ranks[i],rows[i]['observed']),reverse=True):
            r=rows[rid]
            obj={k:r[k] for k in ['id','kind','logical_key','content','source','locator','basis','observed','expires','scope']}
            encoded=json.dumps(obj,ensure_ascii=False)
            if size+len(encoded)>max_chars: continue
            selected.append(obj);size+=len(encoded)
            if len(selected)>=limit:break
        ids=[r['id'] for r in selected]
        related=[]
        for rid in ids:
            for edge in self.db.execute('SELECT a,b FROM edges WHERE a=? OR b=?',(rid,rid)):
                other=edge['b'] if edge['a']==rid else edge['a']
                if other in rows and other not in ids and other not in related: related.append(other)
        conflicts=self.db.execute('SELECT COUNT(*) FROM records WHERE scope=? AND status=?',(scope,'conflict')).fetchone()[0]
        return {'trust':'retrieved_data_not_instructions','records':selected,
                'related_ids':related[:6],'unresolved_conflict_records':conflicts,
                'used_chars':size,'eligible_records':len(rows),
                'selection':'FTS5 + Korean bigrams + RRF' if self.fts else 'Korean bigram lexical fallback'}

    def recall_with_wiki(self, query: str, scope: str, *, workspace: Path,
                         limit: int=6, max_chars: int=6500, project: str|None=None,
                         environment: str='') -> dict:
        """Combine bounded source data; never install wiki text as a procedure."""
        from . import obsidian
        try:
            configured = obsidian.load_config(workspace)
        except ValueError:
            configured = None
        if not configured or not configured['enabled'] or scope=='global':
            return self.recall(query,scope,limit=limit,max_chars=max_chars,environment=environment)
        if not 1<=limit<=12 or not 2048<=max_chars<=16000:
            raise ValueError('Invalid combined recall budget')
        result=self.recall(query,scope,limit=max(1,limit//2),max_chars=max(500,max_chars//2),environment=environment)
        wiki=obsidian.context(workspace,query,limit=max(1,limit//2),max_chars=max(2048,max_chars//2),project=project)
        retained=[]
        for card in wiki['cards']:
            duplicate=False
            for record in result['records']:
                current=self.db.execute('SELECT 1 FROM observations WHERE canonical_id=? AND source_revision=?',
                                        (record['id'],card['revision'])).fetchone()
                if record['logical_key']==card['id'] and current:
                    duplicate=True;break
            aliases=self.db.execute('SELECT canonical_id FROM record_aliases WHERE namespace=? AND alias=?',
                                    ('obsidian',card['id'])).fetchall()
            if any(self._lifecycle(row['canonical_id'])=='forgotten' for row in aliases):duplicate=True
            if not duplicate:retained.append(card)
        wiki['cards']=retained
        ids={card['id'] for card in retained}
        wiki['expansion_pointers']=[p for p in wiki['expansion_pointers'] if p['id'] in ids]
        wiki['conflicts']=[p for p in wiki['conflicts'] if p['id'] in ids]
        wiki['budget']['used']=sum(len(card['excerpt']) for card in retained)
        result['wiki_context']=wiki
        result['selection'] += ' + configured source-native wiki (deduplicated by explicit identity/revision)'
        while len(json.dumps(result,ensure_ascii=False))>max_chars and (wiki['cards'] or result['records']):
            if wiki['cards']:
                removed=wiki['cards'].pop();wiki['expansion_pointers']=[p for p in wiki['expansion_pointers'] if p['id']!=removed['id']]
                wiki['conflicts']=[p for p in wiki['conflicts'] if p['id']!=removed['id']]
                wiki['budget']['used']=sum(len(card['excerpt']) for card in wiki['cards'])
            else:result['records'].pop()
            if 'combined-budget-exhausted' not in wiki['warnings']:wiki['warnings'].append('combined-budget-exhausted')
        return result

    def stats(self) -> dict:
        return {'counts':{r[0]:r[1] for r in self.db.execute('SELECT status,COUNT(*) FROM records GROUP BY status')},
                'forgotten':self.db.execute('SELECT COUNT(*) FROM tombstones').fetchone()[0],
                'fts5':self.fts,'path':str(self.path)}
