"""Immutable human-review versions for conversation workspaces."""
import hashlib
import json
import time

from .languages import INITIAL_STYLES, guidance_language_error
from .store import ConfigConflict, atomic_write, uid


class ReviewStore:
    def __init__(self, root):
        super().__init__(root)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS human_reviews (
                id TEXT PRIMARY KEY, project TEXT NOT NULL, kind TEXT NOT NULL,
                root TEXT NOT NULL, parent TEXT, version INTEGER NOT NULL,
                name TEXT NOT NULL, file_id TEXT NOT NULL UNIQUE, content TEXT,
                status TEXT NOT NULL DEFAULT 'pending', job TEXT, style_id TEXT,
                digest TEXT NOT NULL, created REAL NOT NULL, approved REAL
            );
            CREATE INDEX IF NOT EXISTS review_project ON human_reviews(project, kind, created);
            CREATE TABLE IF NOT EXISTS review_migrations (project TEXT PRIMARY KEY);
        """)
        columns = {r[1] for r in self.db.execute('PRAGMA table_info(human_reviews)')}
        for name in ('glossary_id', 'entries'):
            if name not in columns:
                self.execute(f'ALTER TABLE human_reviews ADD COLUMN {name} TEXT')
        for project in self.rows('SELECT id FROM projects'):
            pid = project['id']
            if self.rows('SELECT project FROM review_migrations WHERE project=?', (pid,)):
                continue
            work = self.workspace(pid)
            if not work.exists():
                continue
            style = work / 'style.md'
            if style.is_file() and not self.rows("SELECT id FROM human_reviews WHERE project=? AND kind='style'", (pid,)):
                self.create_style(pid, '历史风格', style.read_text())
            for file in self.rows("SELECT * FROM files WHERE project=? AND kind IN ('output','edited')", (pid,)):
                if not self.review_for_file(pid, file['id']):
                    self.register_review(pid, 'translation', file['name'], file['id'])
            self.execute('INSERT OR IGNORE INTO review_migrations VALUES (?)', (pid,))

    def review(self, pid, rid):
        rows = self.rows('SELECT * FROM human_reviews WHERE project=? AND id=?', (pid, rid))
        if not rows:
            raise ValueError('审核版本不存在')
        return rows[0]

    def review_for_file(self, pid, fid):
        rows = self.rows('SELECT * FROM human_reviews WHERE project=? AND file_id=?', (pid, fid))
        return rows[0] if rows else None

    def register_review(self, pid, kind, name, fid, content=None, job=None, parent=None, style_id=None, glossary_id=None, entries=None):
        previous = self.review(pid, parent) if parent else None
        if previous and previous['kind'] != kind:
            raise ValueError('版本类型不匹配')
        rid = uid()
        root = previous['root'] if previous else rid
        version = self.rows('SELECT COALESCE(MAX(version),0)+1 AS n FROM human_reviews WHERE root=?', (root,))[0]['n']
        file = self.file(pid, fid)
        digest = hashlib.sha256(self.download_path(pid, file).read_bytes()).hexdigest()
        self.execute("""INSERT INTO human_reviews
            (id,project,kind,root,parent,version,name,file_id,content,job,style_id,glossary_id,entries,digest,created)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (rid,pid,kind,root,parent,version,name,fid,content,job,
             style_id if style_id is not None else previous['style_id'] if previous else None,
             glossary_id if glossary_id is not None else previous['glossary_id'] if previous else None,
             json.dumps(entries, ensure_ascii=False) if entries is not None else None,digest,time.time()))
        self.event(pid, job, 'progress', f'{name} · v{version} 已生成，等待人工审核')
        return self.review(pid, rid)

    def create_style(self, pid, name, content, job=None, parent=None):
        if not content.strip():
            raise ValueError('风格内容不能为空')
        path = self.workspace(pid) / 'styles'
        path.mkdir(exist_ok=True)
        path = path / (uid() + '.md')
        atomic_write(path, content)
        fid = self.add_file(pid, name + '.md', 'style', path)
        return self.register_review(pid, 'style', name, fid, content, job, parent)

    def create_glossary(self, pid, name, rows, job=None, parent=None):
        from .glossary_documents import validate_entries, markdown
        rows = validate_entries(rows)
        content = markdown(rows)
        directory = self.workspace(pid) / 'glossaries'
        directory.mkdir(exist_ok=True)
        path = directory / (uid() + '.md')
        atomic_write(path, content)
        fid = self.add_file(pid, name + '.md', 'glossary', path)
        return self.register_review(pid, 'glossary', name, fid, content, job, parent, entries=rows)

    def approved_glossaries(self, pid):
        return self.rows("""SELECT r.* FROM human_reviews r WHERE project=? AND kind='glossary' AND status='approved'
            AND NOT EXISTS (SELECT 1 FROM human_reviews n WHERE n.root=r.root AND n.status='approved' AND n.version>r.version)
            ORDER BY created DESC""", (pid,))

    def approve_review(self, pid, rid):
        row = self.review(pid, rid)
        file = self.file(pid, row['file_id'])
        if hashlib.sha256(self.download_path(pid, file).read_bytes()).hexdigest() != row['digest']:
            raise ConfigConflict('文件内容已变化，请重新生成审核版本')
        if row['kind'] == 'style':
            error = guidance_language_error(row['content'], self.project(pid)['target_language'], required=True)
            if error:
                raise ValueError('风格语言不符合项目目标语言，请修改后再审核：' + error)
        if row['status'] != 'approved':
            newer = self.rows("SELECT id FROM human_reviews WHERE root=? AND version>? AND status='approved'", (row['root'],row['version']))
            if newer:
                raise ConfigConflict('已有更新的已审核版本，请使用新版本')
            self.execute("UPDATE human_reviews SET status='approved',approved=? WHERE project=? AND id=? AND status='pending'",
                         (time.time(),pid,rid))
            self.event(pid, row['job'], 'progress', f"{row['name']} · v{row['version']} 人工审核通过")
        return self.review(pid, rid)

    def approved_styles(self, pid):
        return self.rows("""SELECT r.* FROM human_reviews r WHERE project=? AND kind='style' AND status='approved'
            AND NOT EXISTS (SELECT 1 FROM human_reviews n WHERE n.root=r.root AND n.status='approved' AND n.version>r.version)
            ORDER BY created DESC""", (pid,))

    def require_approved_input(self, pid, fid):
        file = self.file(pid, fid)
        review = self.review_for_file(pid, fid)
        if file['kind'] in ('output','edited','style','glossary') or review:
            if not review or review['status'] != 'approved':
                raise ConfigConflict('这份文档尚未人工审核通过，请先审核，再进行排版或事实核查')
            if hashlib.sha256(self.download_path(pid, file).read_bytes()).hexdigest() != review['digest']:
                raise ConfigConflict('已审核文件内容已变化，请重新审核新版本')

    def latest_approved_translation(self, pid):
        rows = self.rows("SELECT file_id FROM human_reviews WHERE project=? AND kind='translation' AND status='approved' ORDER BY approved DESC LIMIT 1", (pid,))
        return rows[0]['file_id'] if rows else None

    def enqueue(self, pid, kind, payload):
        payload = dict(payload)
        if kind == 'translate':
            rid = payload.get('style_version_id')
            if rid == 'generic':
                style = None
            elif rid:
                style = self.review(pid, rid)
                if style['kind'] != 'style' or style['status'] != 'approved':
                    raise ConfigConflict('请选择已审核通过的翻译风格')
            else:
                choices = self.approved_styles(pid)
                if len(choices) > 1:
                    raise ValueError('有多份已审核风格，请选择本次翻译风格')
                style = choices[0] if choices else None
            payload['style_version_id'] = style['id'] if style else 'generic'
            payload['_style_content'] = style['content'] if style else INITIAL_STYLES[self.project(pid)['target_language']]
            payload['_style_name'] = style['name'] if style else '通用翻译风格（系统默认）'
            gid = payload.get('glossary_version_id')
            if gid:
                glossary = self.review(pid, gid)
                if glossary['kind'] != 'glossary' or glossary['status'] != 'approved':
                    raise ConfigConflict('请选择已审核通过的对照词表')
            else:
                choices = self.approved_glossaries(pid)
                if len(choices) > 1:
                    raise ValueError('有多份已审核对照词表，请选择本次翻译词表')
                glossary = choices[0] if choices else None
            payload['glossary_version_id'] = glossary['id'] if glossary else None
            if glossary:
                self.require_approved_input(pid, glossary['file_id'])
                payload['_glossary_name'] = glossary['name']
        if kind in ('layout','factcheck'):
            for fid in payload.get('file_ids', []):
                self.require_approved_input(pid, fid)
        return super().enqueue(pid, kind, payload)

    def publish_translation(self, pid, source_file, output, paragraphs, job, parent_file=None):
        fid = super().publish_translation(pid, source_file, output, paragraphs, job, parent_file)
        payload = json.loads(self.rows('SELECT payload FROM jobs WHERE id=?', (job,))[0]['payload'])
        previous = self.review_for_file(pid, parent_file) if parent_file else None
        self.register_review(pid, 'translation', output.name, fid, job=job,
                             parent=previous['id'] if previous else None, style_id=payload.get('style_version_id'), glossary_id=payload.get('glossary_version_id'))
        return fid

    def remove_data(self, paths, statements):
        # Keep review deletion in the same transaction as the existing project cleanup.
        statements = list(statements)
        for sql, args in list(statements):
            if sql == 'DELETE FROM projects WHERE id=?':
                statements[:0] = [('DELETE FROM human_reviews WHERE project=?', args),
                                  ('DELETE FROM review_migrations WHERE project=?', args)]
        return super().remove_data(paths, statements)
