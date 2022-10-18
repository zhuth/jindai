"""Database access functionality"""
import hashlib
import json
import re
import jieba
import datetime
from bson import SON
from PyMongoWrapper import MongoOperand, F, Fn, Var, ObjectId, QueryExprParser

from .models import Paragraph, Term


def _expr_groupby(params):
    if isinstance(params, MongoOperand):
        params = params()
    if 'id' in params:
        params['_id'] = {k[1:]: k for k in params['id']}
        del params['id']
    
    # get field name
    field, *_ = params['_id']
    field = field.lstrip('$')
    
    return [
        Fn.group(orig=Fn.first('$$ROOT'), **params),
        Fn.replaceRoot(newRoot=Fn.mergeObjects(
            '$orig', {'group_id': '$_id'}, {k: f'${k}' for k in params if k != '_id'})),
        Fn.addFields(group_field=field)
    ]


def _object_id(params):
    if isinstance(params, MongoOperand):
        params = params()
    if isinstance(params, str):
        return ObjectId(params)
    if isinstance(params, datetime.datetime):
        return ObjectId.from_datetime(params)
    return ObjectId()


def _begins_with(params):
    if isinstance(params, MongoOperand):
        params = params()
    if not isinstance(params, str):
        params = str(params)
    return F.keywords.regex(f'^{re.escape(params)}')()


def _gid(params):
    '''
    => addFields(gid=filter(input=$keywords,as=t,cond=regexMatch(input=$$t,regex=`^\*`)))
    => unwind(path=$gid,preserveNullAndEmptyArrays=true)
    => addFields(gid=ifNull($gid;concat('id="';toString($_id));'"'),images=ifNull($images;[]))
    => groupby(id=$gid,pdate=max($pdate),count=sum(size($images)),images=push($images))
    '''
    if isinstance(params, MongoOperand):
        params = params()
    if not isinstance(params, dict):
        params = {}
    return [
        Fn.addFields(
            gid=Fn.filter(input=Var.keywords, as_='t', cond=Fn.regexMatch(
                input="$$t", regex=r'^\*'))
        ),
        Fn.unwind(path=Var.gid, preserveNullAndEmptyArrays=True),
        Fn.addFields(gid=Fn.ifNull(
            Var.gid, Fn.concat('id=', Fn.toString('$_id')))),
    ]


def _sort(params):

    def _rectify(params):
        if isinstance(params, MongoOperand):
            params = params()
        if isinstance(params, dict):
            return params
        if isinstance(params, str):
            params = parser.parse_sort(params)
        if isinstance(params, list):
            return SON(params)
        return params

    return Fn.sort(_rectify(params))()


def _auto(param):

    def _judge_type(query):
        """Judge query mode: keywords or expression"""

        is_expr = False
        if query.startswith('?'):
            is_expr = True
        elif re.search(r'[,.~=&|()><\'"`@_*\-%]', query):
            is_expr = True

        return is_expr

    is_expr = _judge_type(param)
    if is_expr:
        param = param.lstrip('?')
    else:
        param = '`' + '`,`'.join([_.strip().lower().replace('`', '\\`')
                                  for _ in jieba.cut(param) if _.strip()]) + '`'
        if param == '``':
            param = ''

    return param


def _term(param):
    terms = []
    for r in Term.query(F.term == param, F.aliases == param, logic='or'):
        terms += [r.term] + r.aliases
    if terms and len(terms) > 1:
        return {'$in': terms}
    return param


parser = QueryExprParser(
    abbrev_prefixes={None: 'keywords=', '?': 'source.url%', '??': 'content%'},
    allow_spacing=True,
    functions={
        'groupby': _expr_groupby,
        'object_id': _object_id,
        'expand': lambda *x: [
            Fn.unwind('$images')(),
            Fn.lookup(from_='mediaitem', localField='images',
                      foreignField='_id', as_='images')(),
            Fn.sort(SON({'images.source': 1}))
        ],
        'bytes': bytes.fromhex,
        'begin': _begins_with,
        'sort': _sort,
        'gid': _gid,
        'auto': _auto,
        'term': _term
    },
    force_timestamp=False,
)


class DBQuery:
    """Database query class"""

    @staticmethod
    def _parse(query, wordcutter=None):
        """Parse query (keywords or expression) and convert it to aggregation query"""

        if wordcutter is None:
            wordcutter = jieba.cut

        if not query:
            return []

        if isinstance(query, (tuple, list)):
            query, *limitations = query
        else:
            limitations = []

        # parse limitations
        if limitations and limitations[0]:
            limitations = {
                '$and': [parser.parse(expr) for expr in limitations]}

        if isinstance(query, str):
            # judge type of major query and formulate
            query = _auto(query)
            # parse query
            query = parser.parse(query) or []

        if not isinstance(query, list):
            query = [query]

        if not query:
            return [{'$match': limitations}] if limitations else []

        for i in range(len(query)):
            stage = query[i]
            if isinstance(stage, str):
                query[i] = {'$match': parser.parse(stage)}
            elif isinstance(stage, dict) and \
                    not [_ for _ in stage if _.startswith('$') and _ not in ('$and', '$or')]:
                query[i] = {'$match': stage}

        query = DBQuery._merge_req(query, limitations)
        return query

    @staticmethod
    def _merge_req(qparsed, req):
        """Merge to parsed expressions"""
        if not req:
            return qparsed

        first_query = qparsed[0]
        if isinstance(first_query, dict) and '$match' in first_query:
            return [
                {'$match':
                    (MongoOperand(first_query['$match']) & MongoOperand(req))()}
            ] + qparsed[1:]

        return qparsed + [{'$match': req}]

    def __init__(self, query, mongocollections='', limit=0, skip=0, sort='',
                 raw=False, groups='none', pmanager=None, wordcutter=None):

        self.query = DBQuery._parse(query, wordcutter)
        self.raw = raw

        # test plugin pages
        if pmanager and len(self.query) > 0 and '$plugin' in self.query[-1]:
            self.query, plugin_args = self.query[:-1], \
                self.query[-1]['$plugin'].split('/')
        else:
            plugin_args = []

        self.handler = None
        if plugin_args:
            self.handler = pmanager.filters.get(
                plugin_args[0]), plugin_args[1:]

        if not mongocollections:
            mongocollections = ''
        self.mongocollections = mongocollections.split('\n') if isinstance(
            mongocollections, str) else mongocollections

        if sort == 'id':
            sort = ''

        if len(self.query) > 1 and isinstance(self.query[0], str) and self.query[0].startswith('from'):
            self.mongocollections = [self.query[0][4:]]
            self.query = self.query[1:]

        if len(self.query) > 1 and '$raw' in self.query[-1]:
            self.raw = self.query[-1]['$raw']
            self.query = self.query[:-1]

        self.groups = groups

        groupping = ''
        if groups == 'none':
            pass
        elif groups == 'group':
            groupping = '''
                addFields(group_id=filter(input=$keywords,as=t,cond=eq(substrCP($$t;0;1);"*")));
                unwind(path=$group_id,preserveNullAndEmptyArrays=true);
                addFields(group_id=ifNull($group_id;ifNull(concat('id=';toString($_id));$source.file)))
            '''
            if not sort:
                sort = 'group_id,-pdate'
        elif groups == 'source':
            groupping = 'addFields(group_id=ifNull($source.url;$source.file))'
            if not sort:
                sort = 'source'
        else:
            groupping = f'addFields(group_id=${groups})'
            if not sort:
                sort = '-group_id'

        if groupping:
            if '.' not in sort and ',' not in sort:
                if sort.startswith('-'):
                    sorting = f',sorting_field=max(${sort[1:]})'
                else:
                    sorting = f',sorting_field=min(${sort})'
                sorting = sorting.replace('($id)', '($_id)')
                sort = ('-' if sort.startswith('-') else '') + 'sorting_field'
            else:
                sorting = ''
            groupping += f'''
                =>addFields(gid=ifNull($group_id;ifNull(concat('id=';toString($_id));$source.file)),images=ifNull($images;[]))
                =>groupby(id=$gid,count=sum(size($images)),images=push($images){sorting})
                =>groupby(id=$_id)
                =>addFields(
                    images=reduce(input=$images,initialValue=[],in=setUnion($$value;$$this)),
                    keywords=cond(regexMatch(regex=`^\*`,input=toString($group_id));[toString($group_id)];$keywords),
                    group_id=$gid
                )
            '''
            groupping = parser.parse(groupping)

            self.query += groupping

        self.limit = limit
        self.sort = sort or 'id'
        self.skips = {}
        self.skip = skip
        
    @property
    def query_hash(self):
        return hashlib.sha1(json.dumps(self.query).encode('utf-8')).hexdigest()

    def fetch_rs(self, mongocollection, sort=None, limit=-1, skip=-1):
        """Fetch result set for single mongo collection"""

        rs = Paragraph.get_coll(mongocollection)

        if sort is None:
            sort = self.sort
        if skip < 0:
            skip = self.skips.get(mongocollection, 0)
        if limit < 0:
            limit = self.limit

        agg = self.query

        sort = parser.parse_sort(sort)

        if not sort or sort == [('_id', 1)]:
            if not [stage for stage in agg if '$sort' in stage]:
                agg.append({
                    '$sort': SON([('pdate', -1), ('_id', 1)])
                })
        elif sort == [('random', 1)]:
            agg.append({'$sample': {'size': limit}})
            limit = 0
            skip = 0
        else:
            agg.append(
                {'$sort': SON(sort)})
        if skip > 0:
            agg.append({'$skip': skip})
        if limit > 0:
            agg.append({'$limit': limit})
        rs = rs.aggregate(agg, raw=self.raw, allowDiskUse=True)

        return rs

    def fetch_all_rs(self):
        """Fetch all result sets"""

        if self.skip is not None and self.skip > 0:
            skip = self.skip
            for coll in self.mongocollections:
                count = self.fetch_rs(coll, sort='id', limit=0, skip=0).count()
                if count <= skip:
                    skip -= count
                    self.skips[coll] = -1
                else:
                    self.skips[coll] = skip
                    break

        for coll in self.mongocollections:
            if self.skips.get(coll, 0) >= 0:
                yield from self.fetch_rs(coll)

    def fetch(self):
        """Fetch results"""

        if self.handler:
            handler, args = self.handler
            yield from handler['handler'](self, *args)
        else:
            yield from self.fetch_all_rs()

    def count(self):
        """Count documents, -1 if err"""
        try:
            return sum([self.fetch_rs(r, sort='id', limit=0, skip=0).count() for r in self.mongocollections])
        except Exception:
            return -1
