from functools import wraps
from flask import Flask, Response, render_template, jsonify, request, session, send_file, json
from pandas.io.clipboards import read_clipboard
from  geventwebsocket.websocket import WebSocketError
from  geventwebsocket.handler import WebSocketHandler
from  gevent.pywsgi import WSGIServer
from bson import ObjectId
import datetime
import inspect
import logging
import hashlib
import traceback
from PyMongoWrapper import *
from collections import defaultdict
from models import *
from task import Task
from pipeline import Pipeline
import threading
from collections import deque
from pdf2image import convert_from_path
from io import BytesIO
import sys
import config
import logging
import base64
ws_clients = {}


class Token(dbo.DbObject):
    
    user = str
    token = str
    expire = float

    _cache = {}

    @staticmethod
    def check(token_string):
        t = Token._cache.get(token_string)
        if t and t.expire > time.time():
            return t
        else:
            t = Token.first((F.token == token_string) & (F.expire > time.time()))
            if t:
                Token._cache[token_string] = t
                return t
        return None


class TasksQueue:

    def __init__(self):
        self._q = deque() # deque is documented as thread-safe, so no need to use lock.
        self.running = False
        self.running_task = ''
        self.results = {}

    def start(self):
        self.running = True
        self._workingThread = threading.Thread(target=self.working)
        self._workingThread.start()

    @property
    def status(self):
        return {
            'running': self.running_task,
            'finished': [{
                'id': k,
                'name': k.split('/', 1)[-1].split('_')[0],
                'viewable': isinstance(v, list) or (isinstance(v, dict) and 'exception' in v),
                'last_run': datetime.datetime.fromtimestamp(int(k.split('_')[-1])).strftime('%Y-%m-%d %H:%M:%S'),
                'file_ext': 'json' if not isinstance(v, dict) else v.get('__file_ext__', 'json')
            } for k, v in self.results.items()],
            'waiting': len(self)
        }

    def working(self):
        while self.running:
            if self._q:
                self.running_task, t = self._q.popleft()
                # emit('queue', self.status)

                try:
                    task = Task(datasource=(t.datasource, t.datasource_config), pipeline=t.pipeline, concurrent=t.concurrent, resume_next=t.resume_next)
                    # emit('debug', 'task inited') 
                    self.results[self.running_task] = task.execute()
                except Exception as ex:
                    self.results[self.running_task] = {'exception': str(ex), 'tracestack': traceback.format_tb(ex.__traceback__)}
                self.running_task = ''
                
                # emit('queue', self.status)
            else:
                self.running = False

    def enqueue(self, key, val):
        self._q.append((key, val))
        # emit('queue', self.status)

    def stop(self):
        self.running = False

    def __len__(self):
        return len(self._q)

    def remove(self, key):
        for todel in self._q:
            if todel[0] == key: break
        else:
            return False
        self._q.remove(todel)
        # emit('queue', self.status)
        
        return True


app = Flask(__name__)
app.config['SECRET_KEY'] = config.secret_key
je = dbo.create_dbo_json_encoder(json.JSONEncoder)


class NumpyEncoder(json.JSONEncoder):
    def __init__(self, **kwargs):
        kwargs['ensure_ascii'] = False
        super().__init__(**kwargs)

    def default(self, obj):
        import numpy as np
        if isinstance(obj, bytes):
            return f'{base64.b64encode(obj).decode("ascii")}'
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        if isinstance(obj, np.int32):
            return obj.tolist()
        if isinstance(obj, Image.Image):
            return str(obj)
        return je.default(self, obj)


app.json_encoder = NumpyEncoder

tasks_queue = TasksQueue()


def logined():
    t = Token.check(request.headers.get('X-Authentication-Token', request.cookies.get('token')))
    if t:
        return t.user
    

def valid_task(j):

    j = dict(j)
    
    def _valid_args(t, args):
        argnames = inspect.getfullargspec(t.__init__).args[1:]
        toremove = []
        for k in args:
            if k not in argnames or args[k] is None:
                logging.info(k, 'not an arg' if k not in argnames else 'is null')
                toremove.append(k)
        for k in toremove:
            del args[k]

        return args
    
    if 'datasource' not in j:
        j['datasource'] = 'DBQueryDataSource'
    if 'datasource_config' not in j:
        j['datasource_config'] = {}

    _valid_args(Task.datasource_ctx[j['datasource']], j['datasource_config'])

    if 'pipeline' not in j:
        j['pipeline'] = []

    for name, args in j['pipeline']:
        _valid_args(Pipeline.pipeline_ctx[name], args)

    return j


def rest(login=True, cache=False, user_role=''):
    def do_rest(func):
        @wraps(func)
        def wrapped(*args, **kwargs):
            try:
                if (login and not logined()) or \
                    (user_role and not User.first((F.roles == user_role) & (F.username == logined()))):
                    raise Exception('Forbidden.')
                
                f = func(*args, **kwargs)
                if isinstance(f, Response): return f
                resp = jsonify({'result': f})
            except Exception as ex:
                resp = jsonify({'exception': str(ex), 'tracestack': traceback.format_tb(ex.__traceback__)})
            
            resp.headers.add("Access-Control-Allow-Origin", "*")
            if cache:
                resp.headers.add("Cache-Control", "public,max-age=86400")
            return resp
        return wrapped

    return do_rest


@app.route('/api/authenticate', methods=['POST'])
@rest(login=False)
def authenticate():
    j = request.json
    u, p = j['username'], j['password']
    if User.authenticate(u, p):
        Token.query(F.user==u).delete()
        token = User.encrypt_password(str(time.time()), str(time.time_ns()))
        Token(user=u, token=token, expire=time.time() + 86400).save()
        return token
    raise Exception("Wrong user name/password.")


@app.route('/api/authenticate')
@rest()
def whoami():
    if logined():
        u = User.first(F.username == logined()).as_dict()
        del u['password']
        return u
    return None


@app.route('/api/users/')
@app.route('/api/users/<user>', methods=['GET', 'POST'])
@rest(user_role='admin')
def admin_users(user=''):
    if user:
        j = request.json
        u = User.first(F.username == user)
        if not u: return 'No such user.', 404
        if 'password' in j:
            u.set_password(j['password'])
        if 'roles' in j:
            u.roles = j['roles']
        u.save()
    else:
        return list(User.query({}))


@app.route('/api/users/', methods=['PUT'])
@rest(user_role='admin')
def admin_users_add():
    j = request.json
    if User.first(F.username == j['username']):
        raise Exception('User already exists: ' + str(j['username']))
    u = User(username=j['username'])
    u.set_password(j['password'])
    u.save()
    u = u.as_dict()
    del u['password']
    return u


@app.route('/api/account/', methods=['POST'])
@rest()
def user_change_password():
    j = request.json
    u = User.first(F.username == logined())
    assert User.authenticate(logined(), j['old_password']), '原密码错误'
    u.set_password(j['password'])
    u.save()
    u = u.as_dict()
    del u['password']
    return u


@app.route('/api/users/<uname>', methods=['DELETE'])
@rest(user_role='admin')
def admin_users_del(uname):
    return User.query(F.username == uname).delete()


def file_detail(path):
    st = os.stat(path)
    return {
        'name': os.path.basename(path),
        'fullpath': path[len(config.storage):],
        'ctime': st.st_ctime,
        'mtime': st.st_mtime,
        'size': st.st_size,
        'folder': os.path.isdir(path)
    }


@app.route('/api/storage/<path:dir>', methods=['GET'])
@app.route('/api/storage/', methods=['GET'])
@rest()
def list_storage(dir=''):
    dir = os.path.join(config.storage, dir) if dir and not '..' in dir else config.storage
    if os.path.isdir(dir):
        return sorted(map(file_detail, [os.path.join(dir, x) for x in os.listdir(dir)]), key=lambda x: x['ctime'], reverse=True)
    else:
        return send_file(dir)


@app.route('/api/storage/<path:dir>', methods=['PUT'])
@app.route('/api/storage/', methods=['PUT'])
@rest()
def write_storage(dir=''):
    dir = os.path.join(config.storage, dir) if dir and not '..' in dir else config.storage
    sfs = []
    for f in request.files.values():
        sf = os.path.join(dir, f.filename)
        f.save(sf)
        sfs.append(file_detail(sf))
    return sfs


@app.route('/api/paragraphs/<id>', methods=['POST'])
@rest()
def modify_paragraph(id):
    id = ObjectId(id)
    p = Paragraph.first(F.id == id)
    if p:
        for f, v in request.json.items():
            if f in ('_id', 'matched_content'): continue
            if v is None and hasattr(p, f):
                delattr(p, f)
            else:
                setattr(p, f, v)
        p.save()
    return True


@app.route('/api/tasks/', methods=['PUT'])
@rest()
def create_task():
    j = valid_task(request.json)
    task = TaskDBO(**j)
    task.save()
    return task.id


@app.route('/api/tasks/shortcuts', methods=['GET'])
@rest()
def list_tasks_shortcuts():
    return list(TaskDBO.query(F.shortcut_map != {}))

@app.route('/api/tasks/<id>', methods=['DELETE'])
@rest()
def delete_task(id):
    _id = ObjectId(id)
    return TaskDBO.query(F.id == _id).delete()


@app.route('/api/tasks/<id>', methods=['POST'])
@rest()
def update_task(id):
    _id = ObjectId(id)
    j = valid_task(request.json)
    if '_id' in j: del j['_id']
    return {'acknowledged': TaskDBO.query(F.id == _id).update(Fn.set(j)).acknowledged, 'updated': j}


@app.route('/api/tasks/<id>', methods=['GET'])
@app.route('/api/tasks/<offset>/<limit>', methods=['GET'])
@app.route('/api/tasks/', methods=['GET'])
@rest()
def list_task(id='', offset=0, limit=10):
    if id:
        _id = ObjectId(id)
        return TaskDBO.first(F.id == _id)
    else:
        return list(TaskDBO.query({}).sort(-F.last_run, -F.id).skip(int(offset)).limit(int(limit)))


@app.route('/api/queue/', methods=['PUT'])
@rest()
def enqueue_task():
    _id = ObjectId(request.json['id'])
    t = TaskDBO.first(F.id == _id)
    assert t, 'No such task.'
    t.last_run = datetime.datetime.now()
    t.save()
    tasks_queue.enqueue(f'{t.id}/{t.name}_{int(time.time())}', t)
    if not tasks_queue.running:
        logging.info('start background thread')
        tasks_queue.start()
    return _id


@app.route('/api/queue/<path:_id>', methods=['DELETE'])
@rest()
def dequeue_task(_id):
    if _id in tasks_queue.results:
        del tasks_queue.results[_id]
        # emit('queue', tasks_queue.status)
        return True
    else:
        return tasks_queue.remove(_id)


@app.route('/api/queue/<path:_id>', methods=['GET'])
@rest(cache=True)
def fetch_task(_id):        
    if _id not in tasks_queue.results:
        return Response('No such id: ' + _id, 404)
    r = tasks_queue.results[_id]

    if isinstance(r, list):
        offset, limit = int(request.args.get('offset', 0)), int(request.args.get('limit', 100))
        return {
            'results': r[offset:offset+limit],
            'total': len(r)
        }
    elif r is None:
        return None
    else:
        if isinstance(r, dict) and '__file_ext__' in r and 'data' in r:
            buf = BytesIO(r['data'])
            buf.seek(0)
            return send_file(buf, 'application/octstream', as_attachment=True, attachment_filename=os.path.basename(_id + '.' + r['__file_ext__']))
        else:
            return jsonify(r)


@app.route('/api/queue/', methods=['GET'])
@rest()
def list_queue():
    return tasks_queue.status


@app.route('/api/help/<pipe_or_ds>')
@rest(cache=True)
def help(pipe_or_ds):

    def _doc(cl):
        args_docs = {}
        for l in (cl.__init__.__doc__ or '').strip().split('\n'):
            m = re.search(r'(\w+)\s\((.+?)\):\s(.*)', l)
            if m:
                g = m.groups()
                if len(g) > 2:
                    args_docs[g[0]] = {'type': g[1].split(',')[0], 'description': g[2]}

        args_spec = inspect.getfullargspec(cl.__init__)
        args_defaults = dict(zip(reversed(args_spec.args), reversed(args_spec.defaults or [])))

        for arg in args_spec.args[1:]:
            if arg not in args_docs:
                args_docs[arg] = {}
            if arg in args_defaults:
                args_docs[arg]['default'] = json.dumps(args_defaults[arg], ensure_ascii=False)

        return {
            'name': cl.__name__,
            'doc': (cl.__doc__ or '').strip(),
            'args': [
                {'name': k, 'type': v.get('type'), 'description': v.get('description'), 'default': v.get('default')} for k, v in args_docs.items() if 'type' in v
            ]
        }

    ctx = Pipeline.pipeline_ctx if pipe_or_ds == 'pipelines' else Task.datasource_ctx
    r = defaultdict(dict)
    for k, v in ctx.items():
        name = sys.modules[v.__module__].__doc__ or v.__module__.split('.')[-1]
        r[name][k] = _doc(v)
    return r


@app.route('/api/history')
@rest()
def history():
    return list(History.query(F.user == logined()).sort(-F.created_at).limit(100))


@app.route('/api/search', methods=['POST'])
@rest()
def search():

    def _stringify(r):
        if not r: return ''
        if isinstance(r, dict):
            s = []
            for k, v in r.items():
                if k.startswith('$'):
                    s.append(k[1:] + '(' + _stringify(v) + ')')
                else:
                    s.append(k + '=' + _stringify(v))
            if len(s) == 1:
                return s[0]
            else:
                return '(' + ','.join(s) + ')'
        elif isinstance(r, str):
            return '`' + json.dumps(r, ensure_ascii=False)[1:-1].replace('`', '\\`') + '`'
        elif isinstance(r, (int, float)):
            return str(r)
        elif isinstance(r, datetime.datetime):
            return r.strftime('%Y-%m-%d %H:%M:%S')
        elif isinstance(r, list):
            if len(r) == 0:
                return '[]'
            elif len(r) == 1:
                return '[];' + _stringify(r[0])
            else:
                return ';'.join([_stringify(x) for x in r])
        else:
            return '_json(`' + json.dumps(r, ensure_ascii=False) + '`)'

    j = request.json
    q = j.get('q', '')
    req = j.get('req', {})
    sort = j.get('sort', '')
    limit = j.get('limit', 100)
    skip = j.get('offset', 0)
    dataset = j.get('dataset', '')
    
    History(user=logined(), querystr= q + (',' + _stringify(req) if req else ''), created_at=datetime.datetime.now()).save()

    params = {
        'query': q,
        'mongocollection': dataset
    }
    if limit:
        params['limit'] = limit
    if sort:
        params['sort'] = sort
    if req:
        params['req'] = req
    if skip:
        params['skip'] = skip

    task = Task(datasource=('DBQueryDataSource', params), pipeline=[
        ('AccumulateParagraphs', {}),
    ])
    count = task.datasource.count()
    results = [dict(r.as_dict() if isinstance(r, DbObject) else r, dataset=dataset) for r in task.execute()]

    return {'results': results, 'query': task.datasource.querystr, 'total': count}


@app.route('/api/meta')
@rest()
def get_meta():
    return Meta.first({})


@app.route('/api/meta', methods=['POST'])
@rest()
def update_meta():
    j = request.json
    return Meta.query({}).update(Fn.set(j)).acknowledged


@app.route("/api/image")
@rest(cache=True)
def page_image():
    file, pdfpage, storage_id = request.args['file'], int(
        request.args.get('page', '0')), request.args.get('id', '')
    pdfpage += 1
    buf = BytesIO()
    
    if file.endswith('.pdf'):
        file = f'sources/{file}'
        if not os.path.exists(file):
            return 'Not found', 404

        img, = convert_from_path(file, 120, first_page=pdfpage,
                                last_page=pdfpage, fmt='png') or [None]
        if img:
            img.save(buf, format='png')
            buf.seek(0)
        return Response(buf, mimetype='image/png')
    elif file == 'blocks.h5':
        try:
            buf = readonly_storage.read(storage_id)
        except OSError:
            return Response('Not found', 404)
        return Response(buf, mimetype='image/octstream')
    else:
        return Response('Err', 500)


@app.route('/api/quicktask', methods=['POST'])
@rest()
def quick_task():
    j = request.json
    query = j.get('query', '')
    raw = j.get('raw', False)
    mongocollection = j.get('mongocollection', '')

    if query.startswith('datasource='):
        q = parser.eval(query)
        r = Task(datasource=q[0]['datasource'], pipeline=q[1:]).execute()
    else:
        r = Task(datasource=('DBQueryDataSource', {'query': query, 'raw': raw, 'mongocollection': mongocollection}), pipeline=[
            ('AccumulateParagraphs', {}),
        ]).execute()
    
    return r


@app.route('/api/gallery/star', methods=['POST'])
@rest()
def gallery_star():
    j = request.json
    selected = [ObjectId(i) for i in j.get('selected', [])]
    inc = j.get('inc', 1)
    if selected:
        Paragraph.query(F.id.in_(selected)).update(Fn.inc(rating=inc))
        return selected
    else:
        return {'error': 'selected ids are required'}


@app.route('/api/gallery/tag', methods=['POST'])
@rest()
def gallery_tag():
    j = request.json
    selected = [ObjectId(i) for i in j.get('selected', [])]
    field = j.get('field', 'keywords')
    delete = j.get('delete', False)
    fn = Fn.pull if delete else Fn.push
    modified = []
    if selected:
        for tag in j.get('tag', []):
            position = {field: tag}
            modified.append(Paragraph.query(F.id.in_(selected)).update(fn(**position)).modified_count)
    return modified


@app.route('/api/gallery/group', methods=['POST'])
@rest()
def gallery_group():
    j = request.json
    selected = [ObjectId(i) for i in j.get('selected', [])]
    if selected:
        new_group = '#' + hashlib.sha256(str(min(selected)).encode('utf-8')).hexdigest()[-9:]
        groups = [_['_id'] for _ in Paragraph.aggregator.match(F.id.in_(selected)).unwind('$groups').group(_id=Var.groups).perform(raw=True)]
        new_group = min([new_group] + groups)
        Paragraph.query(F.groups.in_(groups)).update(Fn.set(groups=[new_group]))
        return new_group
    return ''
       

@app.route('/api/gallery/delete', methods=['POST'])
@rest()
def gallery_delete():
    j = request.json
    selected = [ObjectId(i) for i in j.get('selected', [])]
    if selected:
        Paragraph.query(F.id.in_(selected)).delete()
        return selected
    else:
        return {'error': 'selected ids are required'}


@app.route('/api/admin/db', methods=['POST'])
@rest(user_role='admin')
def dbconsole():
    mongocollection = request.json['mongocollection']
    query = request.json['query']
    operation = request.json['operation']
    operation_params = request.json['operation_params']
    preview = request.json.get('preview', True)

    mongo = Paragraph.db.database[mongocollection]
    query = parser.eval(query)
    operation_params = parser.eval(operation_params)

    if preview:
        return {
            'mongocollection': mongocollection,
            'query': query,
            'operation': operation,
            'operation_params': operation_params
        }
    else:
        r = getattr(mongo, operation)(query, operation_params)
        if operation == 'update_many':
            r = r.modified_count
        elif operation == 'delete_many':
            r = r.deleted_count
        return r


@app.route('/api/admin/db/collections', methods=['GET'])
@rest(user_role='admin')
def dbconsole_collections():
    return Paragraph.db.database.list_collection_names()


@app.route('/api/socket/<auth>')
def ws_serve(auth):    
    t = Token.check(auth)
    if t:
        ws_clients[auth] = {
            'user': t.user
        }
    else:
        return 'Auth failure', 403
        
    user_socket = request.environ.get("wsgi.websocket")
    if not user_socket:
        return 'HTTP OK, Upgrade'

    print(user_socket)
    while True:
        try:
            
            message = json.loads(user_socket.receive())
            if 'event' not in message: continue
            if message['event'] == 'queue':
                user_socket.send(json.dumps(tasks_queue.status))
            elif message['event'] == 'ping':
                user_socket.send('{"event": "pong"}')
            
        except WebSocketError as e:
            print(e)
            break
    
    if auth in ws_clients:
        del ws_clients[auth]


if __name__ == "__main__":
    # http_serve=WSGIServer(("0.0.0.0", 8370), app, handler_class=WebSocketHandler)
    # http_serve.serve_forever()
    os.environ['FLASK_ENV'] = 'development'
    app.run(debug=True, host='0.0.0.0', port=8370)
