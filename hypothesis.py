#!/usr/bin/env python3
from __future__ import print_function
import json
import pickle
import requests
import traceback
from os import environ

try:
    from urllib.parse import urlencode
except ImportError:
    from urllib import urlencode

try:
    from misc.debug import TDB
    tdb=TDB()
    printD=tdb.printD
except ImportError:
    printD = print

# read environment variables # FIXME not the most modular...

api_token = environ.get('HYP_API_TOKEN', 'TOKEN')  # Hypothesis API token
username = environ.get('HYP_USERNAME', 'USERNAME') # Hypothesis username
group = environ.get('HYP_GROUP', '__world__')
print(api_token, username, group)  # sanity check

# annotation retrieval and memoization

class Memoizer:
    def __init__(self, memoization_file, api_token=api_token, username=username, group=group):
        self.api_token = api_token
        self.username = username
        self.group = group
        self.memoization_file = memoization_file

    def __call__(self):
        return self.get_annos()

    def get_annos_from_api(self, offset=0, limit=None):
        print('yes we have to start from here')
        h = HypothesisUtils(username=self.username, token=self.api_token, group=self.group, max_results=100000)
        params = {'offset':offset,
                  'group':h.group}
        if limit is None:
            rows = h.search_all(params)
        else:
            params['limit'] = limit
            obj = h.search(params)
            rows = obj['rows']
            if 'replies' in obj:
                rows += obj['replies']
        annos = [HypothesisAnnotation(row) for row in rows]
        return annos

    def get_annos_from_file(self):
        if self.memoization_file is not None:
            try:
                with open(self.memoization_file, 'rb') as f:
                    annos = pickle.load(f)
                if annos is None:
                    return []
                else:
                    return annos
            except FileNotFoundError:
                return []
        else:
            return []

    def add_missing_annos(self, annos):
        offset = 0
        limit = 200
        done = False
        while not done:
            new_annos = self.get_annos_from_api(offset, limit)
            offset += limit
            if not new_annos:
                break
            for anno in new_annos:
                if anno not in annos:  # this will catch edits
                    annos.append(anno)
                else:
                    done = True
                    break  # assume that annotations return newest first

    def memoize_annos(self, annos):  # FIXME if there are multiple ws listeners we will have race conditions?
        if self.memoization_file is not None:
            print(f'annos updated, memoizing new version with, {len(annos)} members')
            with open(self.memoization_file, 'wb') as f:
                pickle.dump(annos, f)
        else:
            print(f'No memoization file, not saving.')

    def get_annos(self):
        annos = self.get_annos_from_file()
        if not annos:
            new_annos = self.get_annos_from_api()
            annos.extend(new_annos)
        self.add_missing_annos(annos)
        self.memoize_annos(annos)
        return sorted(annos, key=lambda a: a.updated)

#
# url helpers

def idFromShareLink(link):  # XXX warning this will break
    if 'hyp.is' in link:
        id_ = link.split('/')[3]
        return id_

def shareLinkFromId(id_):
    return 'https://hyp.is/' + id_

# API classes

class HypothesisUtils:
    """ services for authenticating, searching, creating annotations """
    def __init__(self, username=None, token=None, group=None, domain=None, max_results=None, limit=None):
        if domain is None:
            self.domain = 'hypothes.is'
        else:
            self.domain = domain
        if username is not None:
            self.username = username
        if token is not None:
            self.token = token
        self.app_url = 'https://%s/app' % self.domain
        self.api_url = 'https://%s/api' % self.domain
        self.query_url_template = 'https://%s/api/search?{query}' % self.domain
        self.group = group if group is not None else '__world__'
        self.single_page_limit = 200 if limit is None else limit  # per-page, the api honors limit= up to (currently) 200
        self.multi_page_limit = 200 if max_results is None else max_results  # limit for paginated results
        self.permissions = {
                "read": ['group:' + self.group],
                "update": ['acct:' + self.username + '@hypothes.is'],
                "delete": ['acct:' + self.username + '@hypothes.is'],
                "admin":  ['acct:' + self.username + '@hypothes.is']
                }
        self.ssl_retry = 0

    def authenticated_api_query(self, query_url=None):
        try:
           headers = {'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json;charset=utf-8' }
           r = requests.get(query_url, headers=headers)
           obj = json.loads(r.text)
           return obj
        except requests.exceptions.SSLError as e:
            if self.ssl_retry < 5:
                self.ssl_retry += 1
                print('Ssl error at level', self.ssl_retry, 'retrying....')
                return self.authenticated_api_query(query_url)
            else:
                self.ssl_retry = 0
                print(e)
                print(traceback.print_exc())
                return {'ERROR':True, 'rows':tuple()}
        except BaseException as e:
            print(e)
            #print('Request, status code:', r.status_code)  # this causes more errors...
            print(traceback.print_exc())
            return {'ERROR':True, 'rows':tuple()}

    def make_annotation_payload_with_target_using_only_text_quote(self, url, prefix, exact, suffix, text, tags):
        """Create JSON payload for API call."""
        if exact is None:
            target = [{'source':url}]
        else:
            target = [{
                "scope": [url],
                "selector": 
                [{
                    "type": "TextQuoteSelector", 
                    "prefix": prefix,
                    "exact": exact,
                    "suffix": suffix
                },]
            }]
        if text == None:
            text = ''
        if tags == None:
            tags = []
        payload = {
            "uri": url,
            "user": 'acct:' + self.username + '@hypothes.is',
            "permissions": self.permissions,
            "group": self.group,
            "target": target, 
            "tags": tags,
            "text": text
        }
        return payload

    def create_annotation_with_target_using_only_text_quote(self, url=None, prefix=None, 
               exact=None, suffix=None, text=None, tags=None, tag_prefix=None):
        """Call API with token and payload, create annotation (using only text quote)"""
        payload = self.make_annotation_payload_with_target_using_only_text_quote(url, prefix, exact, suffix, text, tags)
        try:
            r = self.post_annotation(payload)
        except:
            print(traceback.print_exc())
            r = None  # if we get here someone probably ran the bookmarklet from firefox or the like
        return r

    def get_annotation(self, id):
        headers = {'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json;charset=utf-8' }
        r = requests.get(self.api_url + '/annotations/' + id, headers=headers)
        return r

    def post_annotation(self, payload):
        headers = {'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json;charset=utf-8' }
        data = json.dumps(payload, ensure_ascii=False)
        r = requests.post(self.api_url + '/annotations', headers=headers, data=data.encode('utf-8'))
        return r

    def patch_annotation(self, id, payload):
        headers = {'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json;charset=utf-8' }
        data = json.dumps(payload, ensure_ascii=False)
        r = requests.patch(self.api_url + '/annotations/' + id, headers=headers, data=data.encode('utf-8'))
        return r

    def delete_annotation(self, id):
        headers = {'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json;charset=utf-8' }
        r = requests.delete(self.api_url + '/annotations/' + id, headers=headers)
        return r

    def search_all(self, params={}):
        """Call search API with pagination, return rows """
        while True:
            obj = self.search(params)
            rows = obj['rows']
            row_count = len(rows)
            if 'replies' in obj:
                rows += obj['replies']
            params['offset'] += row_count
            if params['offset'] > self.multi_page_limit:
                break
            if len(rows) is 0:
                break
            for row in rows:
                yield row

    def search(self, params={}):
        """ Call search API, return a dict """
        if 'offset' not in params:
            params['offset'] = 0
        if 'limit' not in params or 'limit' in params and params['limit'] is None:
            params['limit'] = self.single_page_limit
        query_url = self.query_url_template.format(query=urlencode(params, True))
        obj = self.authenticated_api_query(query_url)
        return obj


class HypothesisAnnotation:
    """Encapsulate one row of a Hypothesis API search."""
    def __init__(self, row):
        self._row = row
        self.type = None
        self.id = row['id']
        self.updated = row['updated'][0:19]
        self.user = row['user'].replace('acct:','').replace('@hypothes.is','')

        if 'uri' in row:    # should it ever not?
            self.uri = row['uri']
        else:
             self.uri = "no uri field for %s" % self.id
        self.uri = self.uri.replace('https://via.hypothes.is/h/','').replace('https://via.hypothes.is/','')

        if self.uri.startswith('urn:x-pdf') and 'document' in row:
            if 'link' in row['document']:
                self.links = row['document']['link']
                for link in self.links:
                    self.uri = link['href']
                    if self.uri.encode('utf-8').startswith(b'urn:') == False:
                        break
            if self.uri.encode('utf-8').startswith(b'urn:') and 'filename' in row['document']:
                self.uri = row['document']['filename']

        if 'document' in row and 'title' in row['document']:
            t = row['document']['title']
            if isinstance(t, list) and len(t):
                self.doc_title = t[0]
            else:
                self.doc_title = t
        else:
            self.doc_title = self.uri
        if self.doc_title is None:
            self.doc_title = ''
        self.doc_title = self.doc_title.replace('"',"'")
        if self.doc_title == '': self.doc_title = 'untitled'

        self.tags = []
        if 'tags' in row and row['tags'] is not None:
            self.tags = row['tags']
            if isinstance(self.tags, list):
                self.tags = [t.strip() for t in self.tags]

        self.text = ''
        if 'text' in row:
            self.text = row['text']

        self.references = []
        if 'references' in row:
            self.type = 'reply'
            self.references = row['references']

        self.target = []
        if 'target' in row:
            self.target = row['target']

        self.is_page_note = False
        try:
            if self.references == [] and self.target is not None and len(self.target) and isinstance(self.target,list) and 'selector' not in self.target[0]:
                self.is_page_note = True
                self.type = 'pagenote'
        except:
            traceback.print_exc()
        if 'document' in row and 'link' in row['document']:
            self.links = row['document']['link']
            if not isinstance(self.links, list):
                self.links = [{'href':self.links}]
        else:
            self.links = []

        self.start = self.end = self.prefix = self.exact = self.suffix = None
        try:
            if isinstance(self.target,list) and len(self.target) and 'selector' in self.target[0]:
                self.type = 'annotation'
                selectors = self.target[0]['selector']
                for selector in selectors:
                    if 'type' in selector and selector['type'] == 'TextQuoteSelector':
                        try:
                            self.prefix = selector['prefix']
                            self.exact = selector['exact']
                            self.suffix = selector['suffix']
                        except:
                            traceback.print_exc()
                    if 'type' in selector and selector['type'] == 'TextPositionSelector' and 'start' in selector:
                        self.start = selector['start']
                        self.end = selector['end']
                    if 'type' in selector and selector['type'] == 'FragmentSelector' and 'value' in selector:
                        self.fragment_selector = selector['value']

        except:
            print(traceback.format_exc())


    @property
    def group(self): return self._row['group']

    @property
    def permissions(self): return {k:v for k,v in self._row['permissions'].items()}

    def __eq__(self, other):
        # text and tags can change, if exact changes then the id will also change
        return self.id == other.id and self.text == other.text and set(self.tags) == set(other.tags)

    def __hash__(self):
        return hash(self.text + self.id)


# HypothesisHelper class customized to deal with replacing
#  exact, text, and tags based on its replies
#  also for augmenting the annotation with distinct fields
#  using annotation-text:exact or something like that... (currently using PROTCUR:annotation-exact which is super akward)
#  eg annotation-text:children to say exactly what the fields are when there needs to be more than one
#  it is possible to figure most of them out from their content but not always
class HypothesisHelper:  # a better HypothesisAnnotation
    """ A wrapper around sets of hypothes.is annotations
        with referential structure an pretty printing. """
    objects = {}  # TODO updates # NOTE: all child classes need their own copy of objects
    _replies = {}
    reprReplies = True
    _embedded = False
    _done_loading = False
    _annos = {}

    @classmethod
    def byId(cls, id_):
        try:
            return next(v for v in cls.objects.values()).getObjectById(id_)
        except StopIteration as e:
            raise Warning(f'{cls.__name__}.objects has not been populated with annotations yet!') from e

    def __new__(cls, anno, annos):
        if not cls._annos:  # much faster (as in O(n**2) -> O(1)) to populate once at the start
            cls._annos.update({a.id:a for a in annos})
            if len(cls._annos) != len(annos):
                print(f'WARNING it seems you have duplicate entries for annos: {len(cls._annos)} != {len(annos)}')
        try:
            self = cls.objects[anno.id]
            if self._text == anno.text and self._tags == anno.tags:
                #printD(f'{self.id} already exists')
                return self
            else:
                #printD(f'{self.id} already exists but something has changed')
                self.__init__(anno, annos)  # just updated the underlying refs no worries
                return self
        except KeyError:
            #printD(f'{anno.id} doesnt exist')
            return super().__new__(cls)

    def __init__(self, anno, annos):
        self._recursion_blocker = False
        self.annos = annos
        self.id = anno.id  # hardset this to prevent shenanigans
        self.objects[self.id] = self
        #if self.objects[self.id] is None:
            #printD('WAT', self.id)
        self._anno = anno
        self.hasAstParent = False
        self.parent  # populate self._replies before the recursive call
        if len(self.objects) == len(annos):
            self.__class__._done_loading = True

    # protect the original annotation from modification
    @property
    def _permissions(self): return self._anno.permissions
    @property
    def _type(self): return self._anno.type
    @property
    def _exact(self): return self._anno.exact
    @property
    def _text(self): return self._anno.text
    @property
    def _tags(self): return self._anno.tags
    @property
    def _updated(self): return self._anno.updated  # amusing things happen if you use self._anno.tags instead...
    @property
    def references(self): return self._anno.references

    # we don't have any rules for how to modify these yet
    @property
    def exact(self): return self._exact
    @property
    def text(self): return self._text
    @property
    def tags(self): return self._tags
    @property
    def updated(self): return self._updated

    def getAnnoById(self, id_):
        try:
            return self._annos[id_]
        except KeyError as e:
            #print('could not find', id_, shareLinkFromId(id_))
            return None

    def getObjectById(self, id_):
        try:
            return self.objects[id_]
        except KeyError as e:
            anno = self.getAnnoById(id_)
            if anno is None:
                #self.objects[id_] = None  # don't do this it breaks the type on objects
                #print('Problem in', self.shareLink)  # must come after self.objects[id_] = None else RecursionError
                if not self._recursion_blocker:
                    if self._type == 'reply':
                        print('Orphaned reply', self.shareLink, f"{self.__class__.__name__}.byId('{self.id}')")
                    else:
                        print('Problem in', self.shareLink, f"{self.__class__.__name__}.byId('{self.id}')")
                return None
            else:
                h = self.__class__(anno, self.annos)
                return h

    @property
    def shareLink(self):
        self._recursion_blocker = True
        if self.parent is not None:
            self._recursion_blocker = False
            return self.parent.shareLink
        else:
            self._recursion_blocker = False
            return shareLinkFromId(self.id)

    @property
    def parent(self):
        if not self.references:
            return None
        else:
            for parent_id in self.references[::-1]:  # go backward to get the direct parent first, slower for shareLink but ok
                parent = self.getObjectById(parent_id)
                if parent is not None:
                    if parent.id not in self._replies:
                        self._replies[parent.id] = set()
                    self._replies[parent.id].add(self)
                    return parent
                else:
                    #printD(f"Parent gone for {self.__class__.__name__}.byId('{self.id}'}")
                    pass

    @property
    def replies(self):
        # for the record, the naieve implementation of this
        # looping over annos everytime is 3 orders of magnitude slower
        if self._done_loading:
            if self.id not in self._replies:
                self._replies[self.id] = set()
            return self._replies[self.id]  # we use self.id here instead of self to avoid recursion on __eq__
        else:
            print('WARNING: Not done loading annos, you will be missing references!')
            return set()


    def __eq__(self, other):
        return (self.id == other.id and
                self.text == other.text and
                set(self.tags) == set(other.tags) and
                self.updated == other.updated)

    def __hash__(self):
        return hash(self.__class__.__name__ + self.id)

    def __lt__(self, other):
        return self.updated < other.updated

    def __gt__(self, other):
        return not self.__lt__(other)

    @property
    def _python__repr__(self):
        return f"{self.__class__.__name__}.byId('{self.id}')"

    def __repr__(self, depth=0, format__repr__for_children=''):
        start = '|' if depth else ''
        t = ' ' * 4 * depth + start

        parent_id =  f"\n{t}parent_id:    {self.parent.id} {self.parent._python__repr__}" if self.parent else ''
        exact_text = f'\n{t}exact:        {self.exact}' if self.exact else ''

        text_align = 'text:         '
        lp = f'\n{t}'
        text_line = lp + ' ' * len(text_align)
        text_text = lp + text_align + self.text.replace('\n', text_line) if self.text else ''
        tag_text =   f'\n{t}tags:         {self.tags}' if self.tags else ''

        replies = ''.join(r.__repr__(depth + 1) for r in self.replies)
        rep_ids = f'\n{t}replies:      ' + ' '.join(r._python__repr__ for r in self.replies)
        replies_text = (f'\n{t}replies:{replies}' if self.reprReplies else rep_ids) if replies else ''
        return (f'\n{t.replace("|","")}*--------------------'
                f"\n{t}{self.__class__.__name__ + ':':<14}{self.shareLink} {self._python__repr__}"
                f'\n{t}user:         {self._anno.user}'
                f'{parent_id}'
                f'{exact_text}'
                f'{text_text}'
                f'{tag_text}'
                f'{replies_text}'
                f'{format__repr__for_children}'
                f'\n{t}____________________')

