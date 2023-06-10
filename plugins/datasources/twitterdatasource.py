import base64
import datetime
import re
import time
import json
from collections import defaultdict
import zipfile, csv
from dateutil.parser import parse as dtparse

import tweepy
from jindai import storage
from jindai.common import DictObject
from jindai.models import MediaItem, Paragraph
from jindai.pipeline import DataSourceStage
from jindai.dbquery import parser, F


STOPCHARS = '!@#$%^&*()-_=+[]\\;"\'/?？！％＃＊（）「」『』【】、；‘。’“”，？'


def twitter_id_from_timestamp(stamp: float) -> int:
    """Get twitter id from timestamp

    Args:
        stamp (float): time stamp in seconds

    Returns:
        int: twitter id representing the timestamp
    """
    return (int(stamp * 1000) - 1288834974657) << 22


def timestamp_from_twitter_id(tweet_id: int) -> float:
    """Get timestamp from tweet id

    Args:
        tweet_id (int): tweet id

    Returns:
        float: timestamp in UTC
    """
    return ((tweet_id >> 22) + 1288834974657) / 1000


def tweet_id_from_media_url(url: str) -> int:
    url = url.split('/')[-1].split('.')[0]
    tweet_id = int.from_bytes(base64.urlsafe_b64decode(url[:12])[:8], 'big')
    return tweet_id


def timestamp_from_media_url(url: str) -> float:
    """Get timestamp from Base64-encoded media url

    Args:
        url (str): _description_

    Returns:
        float: _description_
    """
    return timestamp_from_twitter_id(tweet_id_from_media_url(url))


def _stamp(dtval):
    if isinstance(dtval, str):
        dtval = parser.parse_literal(dtval)
    if isinstance(dtval, datetime.timedelta):
        dtval = datetime.datetime.now() + dtval
    if isinstance(dtval, datetime.datetime):
        return dtval.timestamp()
    elif isinstance(dtval, (int, float)):
        return dtval
    return None


class TwitterDataSource(DataSourceStage):
    """
    Load from social network
    @zhs 导入社交网络信息
    """

    mappings = {
        'author': 'import_username'
    }

    def __init__(self, **params) -> None:
        super().__init__(**params)
        self.imported_authors = set()

    def apply_params(self,
                     dataset_name='',
                     allow_video=False,
                     allow_retweet=True,
                     media_only=True,
                     consumer_key='', consumer_secret='', access_token_key='', access_token_secret='',
                     bearer_token='',
                     import_username='',
                     time_after='', time_before='',
                     skip_existent=True,
                     proxy=''
                     ) -> None:
        """
        Args:
            dataset_name (DATASET):
                Dataset name
                @zhs 数据集名称
            allow_video (bool, optional):
                Allow video
                @zhs 允许导入视频
            allow_retweet (bool, optional):
                Allow retweet
                @zhs 允许导入转发
            media_only (bool, optional):
                Media only
                @zhs 只导入包含媒体内容的 Tweets
            consumer_key (str, optional): API CONSUMER KEY
            consumer_secret (str, optional): API CONSUMER SECRET
            access_token_key (str, optional): API ACCESS TOKEN KEY
            access_token_secret (str, optional): API ACCESS TOKEN SECRET
            bearer_token (str, optional): Bearer Token
            import_username (LINES, optional):
                Import source, blank for timeline
                @zhs 导入的用户名或留空以导入 Timeline
            time_after (str):
                Time after
                @zhs 时间上限
            time_before (str):
                Time before
                @zhs 时间下限
            skip_existent (bool):
                Skip existent tweets
                @zhs 跳过已经导入的 URL
            proxy (str):
                Proxy settings
                @zhs 代理服务器
        """
        self.dataset_name = dataset_name
        self.allow_video = allow_video
        self.allow_retweet = allow_retweet
        self.media_only = media_only
        self.import_username = import_username
        self.time_after = _stamp(time_after) or 0
        self.time_before = _stamp(time_before) or time.time()
        self.api = tweepy.API(tweepy.OAuthHandler(consumer_key, consumer_secret, access_token_key, access_token_secret) if not bearer_token \
                    else tweepy.OAuth2BearerHandler(bearer_token),
                    wait_on_rate_limit=True,
                    proxy=proxy)
        self.skip_existent = skip_existent
        self.imported = set()
        
    def parse_tweet(self, tweet, skip_existent=None) -> Paragraph:
        """Parse twitter status
        Args:
            st (twitter.status): status
        Returns:
            Post: post
        """
        if skip_existent is None:
            skip_existent = self.skip_existent
            
        if isinstance(tweet, tweepy.Tweet):
            if tweet.id in self.imported:
                return
            
            # get media entities
            media_entities = [
                DictObject(media)
                for media in getattr(tweet, 'extended_entities', tweet.entities).get('media', [])
            ]
            if not media_entities and self.media_only:
                return                
            
            self.imported.add(tweet.id)

            tweet_url = f'https://twitter.com/{tweet.user.screen_name}/status/{tweet.id}'
            # get author info
            author = '@' + tweet.user.screen_name
            if not tweet.text:
                tweet.text = tweet.full_text or ''

            para = Paragraph.get(
                F.tweet_id == f'{tweet.id}', tweet_id=f'{tweet.id}', author=author, content=tweet.text)
            para.source = {'url': tweet_url}

            self.logger(tweet_url, 'existent' if para.id else '')

            if skip_existent and para.id:
                return
        
            for media in media_entities:
                if media.video_info:
                    if not self.allow_video:
                        continue  # skip videos
                    url = media.video_info['variants'][-1]['url'].split('?')[
                        0]
                    if url.endswith('.m3u8'):
                        self.logger('found m3u8, pass', url)
                        continue
                else:
                    url = media.media_url_https

                if url:
                    item = MediaItem.get(
                        url, item_type='video' if media.video_info else 'image')

                    if item.id and self.skip_existent:
                        continue

                    if not item.id:
                        item.save()
                        self.logger('... add new item', url)
                    para.images.append(item)  
        
        if isinstance(tweet, Paragraph):
            para = tweet

        # update paragraph fields
        if rt_author := re.match(r'^RT (@[\w_-]*)', para.content):
            para.author = rt_author.group(1)
        
        tweet_id = int(re.search(r'/status/(\d+)$', para.source['url']).group(1))
        
        para.dataset = self.dataset_name
        para.pdate = datetime.datetime.utcfromtimestamp(
            timestamp_from_twitter_id(tweet_id))
        para.tweet_id = f'{tweet_id}'
        para.images = []

        # wash content and keywords
        text = re.sub(r'https?://[^\s]+', '', para.content).strip()
        para.keywords += [t.strip().strip('#') for t in re.findall(
            r'@[a-z_A-Z0-9]+', text) + re.findall(r'[#\s][^\s@]{,10}', text)] + [para.author]
        para.keywords = [_ for _ in para.keywords if _ and _ not in STOPCHARS]
        para.content = text
        para.save()

        self.logger(len(para.images), 'media items')

        return para

    def import_twiimg(self, url: str):
        """Import twitter posts from url strings
        Args:
            url (str): url
        """
        if 'twitter.com' in url and '/status/' in url:
            self.logger(url)

            tweet_id = url.split('/')
            tweet_id = tweet_id[tweet_id.index('status') + 1]

            try:
                tweet = self.api.get_status(tweet_id)
                para = self.parse_tweet(tweet, False)
                if para:
                    yield para
            except Exception as ex:
                self.log_exception(f'Failed to import from {tweet_id}: {url}', ex)
                
        elif url.endswith('.zip'):
            posts = {}
            zipped = zipfile.ZipFile(storage.open(url))
            for fileinfo in zipped.filelist:
                ext = fileinfo.filename.rsplit('.', 1)[1].lower()
                if ext == 'csv':
                    csvdata = zipped.read(fileinfo).decode('utf-8').splitlines()[5:]
                    csvr = csv.reader(csvdata)
                    columns, *lines = csvr
                    # ['Tweet date', 'Action date', 'Display name', 'Username', 'Tweet URL', 'Media type', 'Media URL', 'Saved filename', 'Remarks', 'Tweet content', 'Replies', 'Retweets', 'Likes']
                    for line in lines:
                        logged = dict(zip(columns, line))
                        url = logged['Tweet URL']
                        self.logger(url)
                        if url not in posts:
                            posts[url] = Paragraph.get(url, dataset=self.dataset_name, author=logged['Username'], content=logged['Tweet content'])
                        para = posts[url]
                        if not para.id:
                            para.pdate = dtparse(logged['Tweet date'])
                            para = self.parse_tweet(para)
                            if not para:
                                del posts[url]
                                continue
                            para.save()
                        if logged['Saved filename']:
                            i = MediaItem.get(logged['Media URL'], zipped_file=logged['Saved filename'], item_type=logged['Media type'].lower())
                            i.save()
                            self.logger('......', i.source['url'], i.zipped_file)
                            para.images.append(i)
                
            for para in posts.values():
                para.save()
                for i in para.images:
                    if i.source.get('file'):
                        self.logger(i.id, 'already stored, skip.')
                        continue
                    try:
                        content = zipped.read(i.zipped_file)
                        path = storage.default_path(i.id)
                        with storage.open(path, 'wb') as output:
                            output.write(content)
                            self.logger(i.id, len(content))
                        i.source = {'file': path, 'url': i.source['url']}
                    except KeyError:
                        self.logger(i.zipped_file, 'not found')
                    i.save()
                yield para

    def import_timeline(self, user=''):
        """Import posts of a twitter user, or timeline if blank"""

        params = dict(count=100,
                      exclude_replies=True)

        if user and user.startswith('@'):
            def source(max_id):
                return self.api.user_timeline(
                    screen_name=user, max_id=max_id-1,
                    include_rts=self.allow_retweet,
                    **params)
        elif user and user.startswith('#'):
            def source(max_id):
                return self.api.user_timeline(
                    user_id=user[1:],
                    max_id=max_id-1,
                    include_rts=self.allow_retweet,
                    **params)
        else:
            def source(max_id):
                return self.api.home_timeline(
                    max_id=max_id-1,
                    **params)

        if self.time_before < self.time_after:
            self.time_before, self.time_after = self.time_after, self.time_before

        max_id = twitter_id_from_timestamp(self.time_before)+1
        before = self.time_before

        self.logger('import timeline', user,
                    self.time_before, self.time_after)

        try:
            pages = 0
            min_id = max(0, twitter_id_from_timestamp(self.time_after))
            while before >= self.time_after and pages < 50:
                pages += 1
                yielded = False
                has_data = False
                self.logger(max_id, datetime.datetime.fromtimestamp(
                    before), self.time_after)

                timeline = source(max_id)
                for status in timeline:

                    if min_id > status.id:
                        break

                    if max_id > status.id:
                        has_data = True
                        before = min(
                            before, timestamp_from_twitter_id(status.id))
                        max_id = min(max_id, status.id)

                    try:
                        para = self.parse_tweet(status)
                    except Exception as exc:
                        self.log_exception('parse tweet error', exc)
                        para = None

                    if para:
                        yield para
                        yielded = True

                if (not user and not yielded) or not has_data:
                    break

                time.sleep(1)

            if pages >= 50:
                self.logger(f'Reached max pages count, interrupted. {user}')

        except tweepy.TweepyException as ex:
            self.log_exception('twitter exception', ex)
            Paragraph(keywords=['!imported', 'error'], author=user, source={'url': f'https://twitter.com/{user[1:]}/status/---imported---error--'}, content=str(ex)).save()

        if user.startswith('@'):
            # save import status
            Paragraph.query(F.keywords == '!imported', F.author == user).delete()
            Paragraph(keywords=['!imported'], author=user, source={'url': f'https://twitter.com/{user[1:]}/status/---imported---'}).save()

    def before_fetch(self, instance):
        instance.imported_authors = self.imported_authors
        time.sleep(3)

    def fetch(self):
        args = self.import_username.split('\n')
        if args == ['']:
            yield from self.import_timeline()
        else:
            for arg in args:
                if re.search(r'^https://twitter.com/.*?/status/.*', arg):
                    yield from self.import_twiimg(arg)
                elif arg.endswith('.zip'):
                    self.logger('import from zip:', arg)
                    for path in storage.globs(arg, False):
                        self.logger('...', path)
                        yield from self.import_twiimg(path)
                elif arg == '@':
                    unames = sorted(
                        map(lambda x: f'#{x}', tweepy.Cursor(self.api.get_friend_ids).items()))
                    for u in unames:
                        self.logger(u)
                        yield from self.import_timeline(u)
                else:
                    if matcher := re.match(r'^https://twitter.com/([^/]*?)(/media)?', arg):
                        arg = '@' + matcher.group(1)
                        if matcher.group(2):
                            self.media_only = True
                    self.imported_authors.add(arg)
                    yield from self.import_timeline(arg)

    def summarize(self, _):
        if imported := self.params.get('import_username'):
            imported = 'author=in(' + json.dumps(imported.split('\n')) + ')'
            return self.return_redirect('/?q=' + imported)
