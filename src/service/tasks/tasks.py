# encoding: utf-8
import datetime
import json
import logging
import re
import os
import hashlib
from abc import abstractmethod
from collections import OrderedDict
from time import sleep, time
from urllib.parse import urljoin
from http import cookies

from pyquery import PyQuery
import requests
from requests.exceptions import TooManyRedirects

import db
from db import dbo
from setting import settings
from .exceptions import *


DOUBAN_URL = 'https://www.douban.com/'
REQUEST_TIMEOUT = 5
REQUEST_RETRY_TIMES = 5
FAKE_API_KEY = '04f1ddfc67bddc4a0ed599f5373994de'

# type: {music|book|movie}; status: {mark|doing|done}
URL_INTERESTS_API = 'https://m.douban.com/rexxar/api/v2/user/{uid}/interests?type={type}&status={status}&start={{start}}&count=50&ck={ck}&for_mobile=1'


class Task:
    """
    工作任务
    """
    _id = 1
    _name = '任务'

    def __init__(self, account):
        class_type = type(self)
        self._name = '{name}#{id}'.format(name=class_type._name, id=class_type._id)
        class_type._id += 1

        self._account = account

    @property
    def name(self):
        return self._name

    def __str__(self):
        return self.name

    def __call__(self, **kwargs):
        self._settings = kwargs
        self._proxy = {
            'http': kwargs['proxy'],
            'https': kwargs['proxy'],
        } if 'proxy' in kwargs else None
        requests_per_minute = kwargs['requests_per_minute']
        self._min_request_interval = 60 / requests_per_minute
        self._local_object_duration = kwargs['local_object_duration']
        self._broadcast_incremental_backup = kwargs['broadcast_incremental_backup']
        self._image_local_cache = kwargs['image_local_cache']
        self._broadcast_active_duration = kwargs['broadcast_active_duration']
        self._last_request_at = time()
        session = requests.Session()
        session.headers.update({
            'Cookie': self._account.session,
            'User-Agent': 'Mozilla/5.0 (Windows NT 6.1; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/60.0.3112.105 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8',
            'Accept-Encoding': 'gzip, deflate, br',
            'Accept-Language': 'zh-CN,zh;q=0.8',
            'Referer': 'https://www.douban.com/',
            'Pragma': 'no-cache',
            'Cache-Control': 'no-cache',
        })
        self._request_session = session

        cookie = cookies.SimpleCookie()
        cookie.load(self._account.session)
        self._account_cookie = cookie

        try:
            return self.run()
        except (TooManyRedirects, Forbidden):
            logging.debug('Login session maybe forbidden')
            self._account.is_invalid = True
            self._account.save()
            return False
        #except Exception as e:
        #    logging.debug(e)
        #    return False
        finally:
            session.close()
    
    def is_oject_expired(self, obj):
        now = datetime.datetime.now()
        return (now - obj.updated_at).seconds > self._local_object_duration

    def get_setting(self, name, default=None):
        return self._settings.get(name, default)

    def fetch_url_content(self, url, base_url=DOUBAN_URL):
        url = urljoin(base_url, url)

        error_count = 0
        while error_count < REQUEST_RETRY_TIMES:
            now = time()
            remaining = self._min_request_interval + self._last_request_at - now
            if remaining > 0:
                sleep(remaining)
            self._last_request_at = now

            try:
                logging.info('fetch URL {0}'.format(url))
                response = self._request_session.get(url, proxies=self._proxy, timeout=REQUEST_TIMEOUT)
                response.raise_for_status()
                if response.history and response.url.startswith('https://www.douban.com/accounts/login'):
                    response.status_code = 403
                    raise requests.exceptions.HTTPError()
                return response
            except requests.exceptions.HTTPError as e:
                if response.status_code == 403:
                    db.Account.update(is_invalid=True).where(db.Account.id == self.account.id).execute()
                    raise Exception('登录凭证失效，请重新登录')
                logging.error('fetch URL "{0}" error, response code: {1}'.format(url, response.status_code))
                break
            except Exception as e:
                error_count += 1
                logging.warn('fetch URL "{0}" error: {1}'.format(url, e))

        logging.error('fetch URL "{0}" error: retries exceeded'.format(url))

    @dbo.atomic()
    def fetch_attachment(self):
        """
        将附件下载到本地
        """
        def prepare_file(url, retries):
            _, file_ext = os.path.splitext(url)
            hash_str = hashlib.md5('{0}|{1}'.format(retries, url).encode()).hexdigest()
            file_path = '{0}/{1}'.format(hash_str[0:2], hash_str[2:4])

            local_filename = '{0}/{1}'.format(file_path, hash_str[4:] + file_ext)
            cache_path = settings.get('cache')
            directory = '{0}/{1}'.format(cache_path, file_path)
            full_path_filename = '{0}/{1}'.format(cache_path, local_filename)
            if not os.path.exists(directory):
                os.makedirs(directory)

            return full_path_filename, local_filename

        try:
            attachment = db.Attachment.get(db.Attachment.local == None)
        except db.Attachment.DoesNotExist:
            return False

        max_retries = 100
        retries = 0
        while retries < max_retries:
            url = attachment.url
            filename, local_filename = prepare_file(url, retries)
            try:
                with open(filename, 'xb') as f:
                    logging.info('download url: {0}'.format(url))
                    response = requests.get(url)
                    for chunk in response.iter_content(chunk_size=1024): 
                        if chunk:
                            f.write(chunk)
            except FileExistsError:
                pass

            break

        if retries == max_retries:
            logging.warn('创建缓存文件失败')
            return False

        try:
            db.Attachment.update(local=local_filename).where(
                db.Attachment.id == attachment.id,
                db.Attachment.local == None
            ).execute()
        except db.IntegrityError:
            pass

        return local_filename


    @property
    def account(self):
        return self.sync_account()

    @abstractmethod
    def run(self):
        raise NotImplementedError()

    def equals(self, task):
        """
        比较两个任务是否相同
        """
        return isinstance(task, type(self)) and self._account.id == task._account.id

    @dbo.atomic()
    def save_user(self, detail):
        """
        用户信息入库
        """
        douban_id = detail['id']
        detail['douban_id'] = douban_id
        detail['unique_name'] = detail['uid']
        detail['version'] = 1
        detail['updated_at'] = datetime.datetime.now()
        del detail['id']
        del detail['uid']

        try:
            user = db.User.safe_create(**detail)
            logging.debug('create user: ' + user.unique_name)
        except db.IntegrityError:
            user = db.User.get(db.User.douban_id == douban_id)
            
            if not user.equals(detail):
                db.UserHistorical.clone(user)
                detail['version'] = db.User.version + 1
                db.User.safe_update(**detail).where(db.User.id == user.id).execute()
        return user

    @dbo.atomic()
    def save_movie(self, detail, douban_id):
        detail['douban_id'] = douban_id
        detail['version'] = 1
        detail['updated_at'] = datetime.datetime.now()
        del detail['id']

        try:
            movie = db.Movie.safe_create(**detail)
            logging.debug('create movie: ' + movie.title)
        except db.IntegrityError:
            movie = db.Movie.get(db.Movie.douban_id == douban_id)
            
            if not movie.equals(detail):
                db.MovieHistorical.clone(movie)
                detail['version'] = db.Movie.version + 1
                db.Movie.safe_update(**detail).where(db.Movie.id == movie.id).execute()
        return movie

    @dbo.atomic()
    def save_book(self, detail):
        douban_id = detail['id']
        detail['douban_id'] = douban_id
        detail['version'] = 1
        detail['updated_at'] = datetime.datetime.now()
        del detail['id']

        try:
            book = db.Book.safe_create(**detail)
            logging.debug('create book: ' + book.title)
        except db.IntegrityError:
            book = db.Book.get(db.Book.douban_id == douban_id)
            
            if not book.equals(detail):
                db.BookHistorical.clone(book)
                detail['version'] = db.Book.version + 1
                db.Book.safe_update(**detail).where(db.Book.id == book.id).execute()
        return book

    @dbo.atomic()
    def save_music(self, detail):
        douban_id = detail['id']
        detail['douban_id'] = douban_id
        detail['version'] = 1
        detail['updated_at'] = datetime.datetime.now()
        del detail['id']

        try:
            music = db.Music.safe_create(**detail)
            logging.debug('create music: ' + music.title)
        except db.IntegrityError:
            music = db.Music.get(db.Music.douban_id == douban_id)
            
            if not music.equals(detail):
                db.MusicHistorical.clone(music)
                detail['version'] = db.Music.version + 1
                db.Music.safe_update(**detail).where(db.Music.id == music.id).execute()
        return music

    def fetch_user(self, name):
        """
        尝试从本地获取用户信息，如果没有则从网上抓取
        """
        try:
            user = db.User.get(db.User.unique_name == name)
            if self.is_oject_expired(user):
                raise db.User.DoesNotExist()
        except db.User.DoesNotExist:
            user = self.fetch_user_by_api(name)

        return user

    def fetch_user_by_id(self, douban_id):
        """
        尝试从本地获取用户信息，如果没有则从网上抓取
        """
        try:
            user = db.User.get(db.User.douban_id == douban_id)
            if self.is_oject_expired(user):
                raise db.User.DoesNotExist()
        except db.User.DoesNotExist:
            user = self.fetch_user_by_api(douban_id)

        return user

    def fetch_user_by_api(self, name):
        """
        通过豆瓣API获取用户信息
        """
        url = 'https://api.douban.com/v2/user/{0}?apikey={1}'.format(name, FAKE_API_KEY)
        response = self.fetch_url_content(url)
        if not response:
            return None

        detail = json.loads(response.text)
        return self.save_user(detail)

    def fetch_movie(self, douban_id):
        """
        尝试从本地获取电影，如果没有则从网上抓取
        """
        try:
            movie = db.Movie.get(db.Movie.douban_id == douban_id)
            if self.is_oject_expired(movie):
                raise db.Movie.DoesNotExist()
        except db.Movie.DoesNotExist:
            movie = self.fetch_movie_by_api(douban_id)

        return movie

    def fetch_movie_by_api(self, douban_id):
        """
        通过豆瓣API获取电影信息
        """
        url = 'https://api.douban.com/v2/movie/{0}?apikey={1}'.format(douban_id, FAKE_API_KEY)
        response = self.fetch_url_content(url)
        if not response:
            return None

        detail = json.loads(response.text)
        return self.save_movie(detail, douban_id)

    def fetch_book(self, douban_id):
        """
        尝试从本地获取书，如果没有则从网上抓取
        """
        try:
            book = db.Book.get(db.Book.douban_id == douban_id)
            if self.is_oject_expired(book):
                raise db.Book.DoesNotExist()
        except db.Book.DoesNotExist:
            book = self.fetch_book_by_api(douban_id)

        return book

    def fetch_book_by_api(self, id):
        """
        通过豆瓣API获取书信息
        """
        url = 'https://api.douban.com/v2/book/{0}?apikey={1}'.format(id, FAKE_API_KEY)
        response = self.fetch_url_content(url)
        if not response:
            return None

        detail = json.loads(response.text)
        return self.save_book(detail)

    def fetch_music(self, douban_id):
        """
        尝试从本地获取音乐，如果没有则从网上抓取
        """
        try:
            music = db.Music.get(db.Music.douban_id == douban_id)
            if self.is_oject_expired(music):
                raise db.Music.DoesNotExist()
        except db.Music.DoesNotExist:
            music = self.fetch_music_by_api(douban_id)

        return music

    def fetch_music_by_api(self, id):
        """
        通过豆瓣API获取书信息
        """
        url = 'https://api.douban.com/v2/music/{0}?apikey={1}'.format(id, FAKE_API_KEY)
        response = self.fetch_url_content(url)
        if not response:
            return None

        detail = json.loads(response.text)
        return self.save_music(detail)

    def sync_account(self):
        """
        同步当前帐号信息
        """
        account = self._account
        user = self.fetch_user(account.name)
        if account.user is None:
            account.user = user
            account.save()
        return account

    def fetch_interests(self, media_type, status):
        interests_list = []
        url = URL_INTERESTS_API.format(
            status=status,
            type=media_type,
            uid=self.account.user.douban_id,
            ck=self._account_cookie['ck'].value
        )
        response = self.fetch_url_content(url.format(start=0))
        result = json.loads(response.text)
        total = result['total']
        interests_list.extend(result['interests'])

        for start in range(50, total, 50):
            response = self.fetch_url_content(url.format(start=start))
            result = json.loads(response.text)
            interests_list.extend(result['interests'])

        return interests_list


class FollowingFollowerTask(Task):
    _name = '备份我的友邻'

    def fetch_follow_list(self, user, action):
        url = 'https://api.douban.com/shuo/v2/users/{user}/{action}?count=50&page={page}'

        user_list = []
        page_count = 1
        while True:
            response = self.fetch_url_content(url.format(action=action, user=user, page=page_count))
            if not response:
                break
            user_list_partial = json.loads(response.text)
            if len(user_list_partial) == 0:
                break
            #user_list.extend([user_detail['uid'] for user_detail in user_list_partial])
            user_list.extend(user_list_partial)
            page_count += 1

        user_list.reverse()
        return user_list

    def fetch_block_list(self):
        response = self.fetch_url_content('https://www.douban.com/contacts/blacklist')
        dom = PyQuery(response.text)
        strip_username = lambda el: re.findall(r'^http(?:s?)://www\.douban\.com/people/(.+)/$', PyQuery(el).attr('href')).pop(0)
        return [strip_username(item) for item in dom('dl.obu>dd>a')]

    @dbo.atomic()
    def save_user_extras(self, user_extras):
        for user, user_extra in user_extras.items():
            now = datetime.datetime.now()
            detail = user_extra.copy()
            detail['updated_at'] = now
            detail['user'] = user
            del detail['id']
            try:
                db.UserExtra.safe_create(**detail)
            except db.IntegrityError:
                del detail['user']
                db.UserExtra.safe_update(**detail).where(db.UserExtra.user == user).execute()

    @dbo.atomic()
    def save_following(self, account_user, following_users):
        now = datetime.datetime.now()
        for following_username, following_user in following_users:
            real_following_username = following_user.unique_name if following_user else following_username
            try:
                kwargs = {
                    'user': account_user,
                    'following_user': following_user,
                    'following_username': real_following_username,
                    'updated_at': now,
                }   
                db.Following(**kwargs).save()
            except db.IntegrityError:
                fw = db.Following.get(
                    db.Following.user == account_user,
                    db.Following.following_username == real_following_username
                )
                if not fw.following_user and following_user is not None :
                    fw.following_user = following_user
                if following_user and fw.following_username != following_user.unique_name:
                    fw.following_username = following_user.unique_name
                fw.updated_at = now
                fw.save()

        db.FollowingHistorical.insert_from(
            db.Following.select(
                db.Following.user,
                db.Following.following_user,
                db.Following.following_username,
                db.Following.created_at,
                db.Following.updated_at,
                db.fn.DATETIME('now')
            ).where(
                db.Following.user == account_user,
                db.Following.updated_at < now
            ),
            [
                db.FollowingHistorical.user,
                db.FollowingHistorical.following_user,
                db.FollowingHistorical.following_username,
                db.FollowingHistorical.created_at,
                db.FollowingHistorical.updated_at,
                db.FollowingHistorical.deleted_at,
            ]
        ).execute()

        db.Following.delete().where(
            db.Following.user == account_user, 
            db.Following.updated_at < now
        ).execute()

    @dbo.atomic()
    def save_followers(self, account_user, followers):
        now = datetime.datetime.now()
        for follower_username, follower in followers:
            try:
                kwargs = {
                    'user': account_user,
                    'follower': follower,
                    'follower_username': follower_username,
                    'updated_at': now,
                }   
                db.Follower(**kwargs).save()
            except db.IntegrityError:
                fw = db.Follower.get(
                    db.Follower.user == account_user,
                    db.Follower.follower_username == follower_username
                )
                if not fw.follower and follower is not None :
                    fw.follower = follower
                fw.updated_at = now
                fw.save()

        db.FollowerHistorical.insert_from(
            db.Follower.select(
                db.Follower.user,
                db.Follower.follower,
                db.Follower.follower_username,
                db.Follower.created_at,
                db.Follower.updated_at,
                db.fn.DATETIME('now')
            ).where(
                db.Follower.user == account_user,
                db.Follower.updated_at < now
            ),
            [
                db.FollowerHistorical.user,
                db.FollowerHistorical.follower,
                db.FollowerHistorical.follower_username,
                db.FollowerHistorical.created_at,
                db.FollowerHistorical.updated_at,
                db.FollowerHistorical.deleted_at,
            ]
        ).execute()

        db.Follower.delete().where(
            db.Follower.user == account_user, 
            db.Follower.updated_at < now
        ).execute()

    @dbo.atomic()
    def save_block_list(self, account_user, block_users):
        now = datetime.datetime.now()
        for block_username, block_user in block_users:
            try:
                kwargs = {
                    'user': account_user,
                    'block_user': block_user,
                    'block_username': block_username,
                    'updated_at': now,
                }   
                db.BlockUser(**kwargs).save()
            except db.IntegrityError:
                new_block_user = db.BlockUser.get(
                    db.BlockUser.user == account_user,
                    db.BlockUser.block_username == block_username
                )
                if not new_block_user.block_user and block_user is not None :
                    new_block_user.block_user = block_user
                new_block_user.updated_at = now
                new_block_user.save()

        db.BlockUserHistorical.insert_from(
            db.BlockUser.select(
                db.BlockUser.user,
                db.BlockUser.block_user,
                db.BlockUser.block_username,
                db.BlockUser.created_at,
                db.BlockUser.updated_at,
                db.fn.DATETIME('now')
            ).where(
                db.BlockUser.user == account_user,
                db.BlockUser.updated_at < now
            ),
            [
                db.BlockUserHistorical.user,
                db.BlockUserHistorical.block_user,
                db.BlockUserHistorical.block_username,
                db.BlockUserHistorical.created_at,
                db.BlockUserHistorical.updated_at,
                db.BlockUserHistorical.deleted_at,
            ]
        ).execute()

        db.BlockUser.delete().where(
            db.BlockUser.user == account_user, 
            db.BlockUser.updated_at < now
        ).execute()

    def run(self):
        account = self.account

        following_user_list = self.fetch_follow_list(account.name, 'following')
        following_users = [(user_detail['uid'], self.fetch_user(user_detail['uid'])) for user_detail in following_user_list]
        self.save_following(account.user, following_users)
        
        follower_list = self.fetch_follow_list(account.name, 'followers')
        follower_users = [(user_detail['uid'], self.fetch_user(user_detail['uid'])) for user_detail in follower_list]
        self.save_followers(account.user, follower_users)

        user_extras = {self.fetch_user(user_detail['uid']): user_detail for user_detail in following_user_list}
        user_extras.update({self.fetch_user(user_detail['uid']): user_detail for user_detail in follower_list})
        self.save_user_extras(user_extras)

        block_list = self.fetch_block_list()
        block_users = [(username, self.fetch_user(username)) for username in block_list]
        self.save_block_list(account.user, block_users)


class InterestsTask(Task):
    @dbo.atomic()
    def save_my_interests(self, subject_name, table, table_historical, user, interests):
        now = datetime.datetime.now()
        for subject_id, interest_detail in interests:
            try:
                interest_detail['created_at'] = now
                interest_detail['updated_at'] = now
                table.safe_create(**interest_detail)
            except db.IntegrityError:
                update_detail = {key: interest_detail[key] for key in ['rating', 'tags', 'create_time', 'comment', 'status']}
                my_interest = table.get(
                    table.user == user,
                    table.subject_id == subject_id
                )
                if my_interest.equals(update_detail):
                    my_interest.updated_at = now
                    my_interest.save()
                else:
                    table_historical.clone(my_interest, {'deleted_at': now})
                    update_detail['updated_at'] = now
                    table.safe_update(**update_detail).where(table.id == my_interest.id).execute()
        
        table_historical.insert_from(
            table.select(
                table.subject_id,
                table.rating,
                table.tags,
                table.create_time,
                table.comment,
                table.status,
                getattr(table, subject_name),
                table.user,
                table.created_at,
                table.updated_at,
                db.fn.DATETIME('now')
            ).where(
                table.user == user,
                table.updated_at < now
            ),
            [
                table_historical.subject_id,
                table_historical.rating,
                table_historical.tags,
                table_historical.create_time,
                table_historical.comment,
                table_historical.status,
                getattr(table_historical, subject_name),
                table_historical.user,
                table_historical.created_at,
                table_historical.updated_at,
                table_historical.deleted_at,
            ]
        ).execute()
        table.delete().where(
            table.user == user, 
            table.updated_at < now
        ).execute()

    def _frodotk_referer_patch(self):
        response = self.fetch_url_content('https://m.douban.com/mine/')
        set_cookie = response.headers['Set-Cookie']
        set_cookie = set_cookie.replace(',', ';')
        cookie = cookies.SimpleCookie()
        cookie.load(set_cookie)
        try:
            patched_cookie = self._account.session + '; frodotk="{0}"'.format(cookie['frodotk'].value)
        except KeyError:
            raise Exception('服务器没有正确授予Cookie，可能是登录会话过期，请重新登录')
        self._request_session.headers.update({
            'Cookie': patched_cookie,
            'Referer': 'https://m.douban.com/',
        })

    def _run(self, subject_name, table, table_historical, fetch_subject):
        '''
        self._request_session.headers.update({
            'Cookie': 'bid=bF6dBPLThxI; frodotk="b1a226a7436be60f14e673f5ca43b22d"; ue="tabris17.cn@hotmail.com"; dbcl2="1832573:JjPMLZuUzTg"; ck=QwA1; ',
            'Referer': 'https://m.douban.com/',
        })
        '''
        self._frodotk_referer_patch()
        account_user = self.account

        wish_list = self.fetch_interests(subject_name, 'mark')
        wish_list.reverse()
        my_wish_mapping = [(item['subject']['id'], {
            'comment': item['comment'],
            'rating': item['rating'],
            'tags': item['tags'],
            'create_time': item['create_time'],
            'status': 'wish',
            'subject_id': item['subject']['id'],
            subject_name: fetch_subject(item['subject']['id']),
            'user': account_user,
        }) for item in wish_list]

        doing_list = self.fetch_interests(subject_name, 'doing')
        doing_list.reverse()
        my_doing_mapping = [(item['subject']['id'], {
            'comment': item['comment'],
            'rating': item['rating'],
            'tags': item['tags'],
            'create_time': item['create_time'],
            'status': 'doing',
            'subject_id': item['subject']['id'],
            subject_name: fetch_subject(item['subject']['id']),
            'user': account_user,
        }) for item in doing_list]

        done_list = self.fetch_interests(subject_name, 'done')
        done_list.reverse()
        my_done_mapping = [(item['subject']['id'], {
            'comment': item['comment'],
            'rating': item['rating'],
            'tags': item['tags'],
            'create_time': item['create_time'],
            'status': 'done',
            'subject_id': item['subject']['id'],
            subject_name: fetch_subject(item['subject']['id']),
            'user': account_user,
        }) for item in done_list]

        self.save_my_interests(
            subject_name,
            table,
            table_historical,
            self.account.user,
            my_wish_mapping + my_doing_mapping + my_done_mapping
        )


class BookTask(InterestsTask):
    _name = '备份我的书'

    def run(self):
        return self._run(
            'book',
            db.MyBook,
            db.MyBookHistorical,
            self.fetch_book
        )


class MovieTask(InterestsTask):
    _name = '备份我的影视'

    def run(self):
        return self._run(
            'movie',
            db.MyMovie,
            db.MyMovieHistorical,
            self.fetch_movie
        )


class MusicTask(InterestsTask):
    _name = '备份我的音乐'

    def run(self):
        return self._run(
            'music',
            db.MyMusic,
            db.MyMusicHistorical,
            self.fetch_music
        )


class BroadcastTask(Task):
    # _conflict_count 超过最大_MAX_CONFLICT_ALLOWED 则认为已经完成增量备份
    # 增量备份满足条件比较苛刻，必须存在连续N条广播都是自己发的且之前备份过，否则永远无法满足条件，升级成完整备份
    # 如果连续转播自己之前的N条广播，也会触发增量备份停止的条件，不过一般不会有人这么干吧
    _MAX_CONFLICT_ALLOWED = 10
    _conflict_count = 0
    _name = '备份我的广播'

    @dbo.atomic()
    def save_status_list(self, statuses):
        current_user = self.account.user
        broadcasts = []
        for status in statuses:
            try:
                broadcasts.append(db.Broadcast.safe_create(**status))
                self._conflict_count = 0
            except db.IntegrityError:
                douban_id = status['douban_id']
                origin_status = db.Broadcast.get(db.Broadcast.douban_id == douban_id)
                if not origin_status.equals(status):
                    update_values = {
                        'reshared_count': status['reshared_count'],
                        'like_count': status['like_count'],
                        'comments_count': status['comments_count'],
                    }
                    db.Broadcast.safe_update(**update_values).where(
                        db.Broadcast.douban_id == douban_id
                    ).execute()
                broadcasts.append(origin_status)
                if current_user.id == origin_status.user.id:
                    # 必须是本人的广播才累计
                    self._conflict_count += 1
                else:
                    self._conflict_count = 0
        return broadcasts

    def fetch_statuses_list(self, now):
        url = self.account.user.alt + 'statuses?p={0}'
        page = 1
        timeline_in_page = []
        def parse_status(status_div):
            """
            关于object_kind说明：
            1001: 图书
            1002: 电影
            1003: 音乐
            1005: 关注好友
            1011: 活动
            1012: 评论
            1013: 小组话题
            1014: （电影）讨论
            1015: 日记
            1018: 图文广播
            1019: 小组
            1020: 豆列
            1021: 九点文章
            1022: 网页
            1025: 相册照片
            1026: 相册
            1043: 影人
            1044: 艺术家
            1062: board(???)
            2001: 线上活动
            2004: 小站视频
            3043: 豆瓣FM单曲
            3049: 读书笔记
            3065: 条目
            3072: 豆瓣FM兆赫
            3090: 东西
            3114: 游戏
            5021: 豆瓣阅读的图片
            5022: 豆瓣阅读的作品

            """
            if not isinstance(status_div, PyQuery):
                status_div = PyQuery(status_div)
            reshared_count = 0
            like_count = 0
            comments_count = 0
            created_at = None
            is_noreply = False
            status_url = None
            target_type = None
            object_kind = None
            object_id = None
            reshared_detail = None
            blockquote = None
            douban_user_id = status_div.attr('data-uid')
            douban_id = status_div.attr('data-sid')
            is_saying = status_div.has_class('saying')
            is_reshared = status_div.has_class('status-reshared-wrapper')
            
            try:
                created_span = status_div.find('.actions>.created_at')[0]
            except:
                is_noreply = True

            try:
                """
                获取广播链接
                """
                exactly_link = PyQuery(status_div.find('.actions a').eq(0))
                status_url = exactly_link.attr('href')
            except:
                pass

            try:
                """
                获取关于广播类型的属性
                """
                status_item_div = PyQuery(status_div.find('.status-item').eq(0))
                target_type = status_item_div.attr('data-target-type')
                object_kind = status_item_div.attr('data-object-kind')
                object_id = status_item_div.attr('data-object-id')
                if not douban_user_id:
                    douban_user_id = status_item_div.attr('data-uid')
                if not douban_id:
                    douban_id = status_div.attr('data-sid')
                blockquote = PyQuery(status_item_div.find('blockquote')).html()
            except:
                pass

            if not is_noreply:
                """
                获取创建时间、回复、点赞、转播数
                """
                try:
                    created_at = PyQuery(created_span).attr('title')
                    reply_link = PyQuery(status_item_div.find('.actions>.new-reply'))
                    comments_count = reply_link.attr('data-count')
                    like_span = PyQuery(status_item_div.find('.actions>.like-count'))
                    like_count = like_span.attr('data-count')
                    if like_count is None:
                        try:
                            like_count = int(re.match(r'赞\((.*)\)', like_span.text().strip())[1])
                        except:
                            like_count = 0
                    reshared_span = PyQuery(status_item_div.find('.actions>.reshared-count'))
                    reshared_count = reshared_span.attr('data-count')
                    if reshared_count is None:
                        reshared_count = 0
                except:
                    pass

            if not douban_id or douban_id == 'None':
                """
                原广播已被删除
                """
                return None, None

            detail = {
                'douban_id': douban_id,
                'douban_user_id': douban_user_id,
                'content': status_div.outer_html(),
                'created': created_at,
                'is_reshared': is_reshared,
                'is_saying': is_saying,
                'is_noreply': is_noreply,
                'updated_at': now,
                'reshared_count': reshared_count,
                'like_count': like_count,
                'comments_count': comments_count,
                'status_url': status_url,
                'target_type': target_type,
                'object_kind': object_kind,
                'object_id': object_id,
                'user': self.fetch_user_by_id(douban_user_id),
                'blockquote': blockquote,
            }

            if is_reshared:
                reshared_status_div = PyQuery(status_div.find('.status-real-wrapper').eq(0))
                reshared_detail, _ = parse_status(reshared_status_div)
                if reshared_detail:
                    detail['reshared_id'] = reshared_detail['douban_id']

            if target_type == 'sns':
                attachments = []
                images = status_div.find('.attachments-saying.group-pics a.view-large')
                for img_lnk in images:
                    attachments.append({
                        'type': 'image',
                        'url': PyQuery(img_lnk).attr('href'),
                    })
                images = status_div.find('.attachments-saying.attachments-pic img')
                for img in images:
                    img_lnk = PyQuery(img).attr('data-raw-src')
                    if img_lnk:
                        attachments.append({
                            'type': 'image',
                            'url': img_lnk,
                        })
                if attachments:
                    self.save_attachments(attachments)
                    detail['attachments'] = attachments
            elif target_type == 'movie' and object_kind == '1002':
                self.fetch_movie(object_id)
            elif target_type == 'book' and object_kind == '1001':
                self.fetch_book(object_id)
            elif target_type == 'music' and object_kind == '1003':
                self.fetch_music(object_id)

            return detail, reshared_detail

        while True:
            response = self.fetch_url_content(url.format(page))
            dom = PyQuery(response.text)
            statuses_in_page = dom('.stream-items>.new-status.status-wrapper')
            if len(statuses_in_page) == 0:
                break
            status_details = []
            reshared_details = []
            for status_wrapper in statuses_in_page:
                status_detail, reshared_detail = parse_status(status_wrapper)
                status_details.append(status_detail)
                if reshared_detail:
                    reshared_details.append(reshared_detail)

            reshared_objects = self.save_status_list(reshared_details)
            reshared_mapping = {_.douban_id: _ for _ in reshared_objects}
            for detail in status_details:
                if 'reshared_id' in detail:
                    detail['reshared'] = reshared_mapping[detail['reshared_id']]
                    del detail['reshared_id']
            status_objects = self.save_status_list(status_details)
            timeline_in_page.extend(status_objects)
            page += 1

            if self._broadcast_incremental_backup and self._conflict_count >= self._MAX_CONFLICT_ALLOWED:
                logging.info('增量备份完成')
                break

        return timeline_in_page

    @dbo.atomic()
    def save_timeline(self, timeline, now):
        timeline_objects = []
        user = self.account.user
        #db.Timeline.delete().where(db.Timeline.user == user).execute()
        for broadcast in timeline:
            try:
                timeline_item = db.Timeline.create(user=user, broadcast=broadcast, updated_at=now)
            except db.IntegrityError:
                timeline_item = db.Timeline.get(
                    db.Timeline.user == user,
                    db.Timeline.broadcast == broadcast
                )
                timeline_item.updated_at = now
                timeline_item.save()
            timeline_objects.append(timeline_item)
        return timeline_objects

    @dbo.atomic()
    def save_attachments(self, attachments):
        attachment_objects = []
        for attachment in attachments:
            try:
                attachment_object = db.Attachment.safe_create(**attachment)
            except db.IntegrityError:
                attachment_object = db.Attachment.get(db.Attachment.url == attachment['url'])
            attachment_objects.append(attachment_object)
        return attachment_objects

    def run(self):
        now = datetime.datetime.now()
        timeline = []
        timeline.extend(self.fetch_statuses_list(now))
        timeline.reverse()
        self.save_timeline(timeline, now)
        if self._image_local_cache:
            while self.fetch_attachment():
                pass


class BroadcastCommentTask(Task):
    _name = '备份广播评论'

    def fetch_comment_list(self, broadcast_url, broadcast_douban_id):
        url = broadcast_url
        comments = []
        while True:
            response = self.fetch_url_content(url)
            dom = PyQuery(response.text)
            comment_items = dom('#comments>.comment-item')
            for comment_item in comment_items:
                item_div = PyQuery(comment_item)
                comments.append({
                    'content': item_div.outer_html(),
                    'target_type': 'broadcast',
                    'target_douban_id': broadcast_douban_id,
                    'douban_id': item_div.attr('data-cid'),
                    'user': self.fetch_user(PyQuery(item_div('.pic>a')).attr('data-uid')),
                    'text': PyQuery(item_div('.content>p.text')).text(),
                    'created': PyQuery(item_div('.content>.author>.created_at')).text(),
                })
            next_page = dom('#comments>.paginator>.next>a')
            if next_page:
                url = broadcast_url + next_page.attr('href')
            else:
                break
        return comments

    @dbo.atomic()
    def save_comment_list(self, comments):
        for detail in comments:
            try:
                db.Comment.safe_create(**detail)
            except db.IntegrityError:
                pass


    def run(self):
        now = datetime.datetime.now()
        active_duration = datetime.timedelta(seconds=self._broadcast_active_duration)
        query = db.Broadcast.select().where(db.Broadcast.created > now - active_duration)
        for row in query:
            try:
                comment_list = self.fetch_comment_list(row.status_url, row.douban_id)
                self.save_comment_list(comment_list)
            except:
                pass


class NoteTask(Task):
    _name = '备份我的日记'

    def run(self):
        pass


class PhotoAlbumTask(Task):
    _name = '备份我的相册'

    def run(self):
        pass


class ReviewTask(Task):
    _name = '备份我的评论'

    def run(self):
        pass


class DoulistTask(Task):
    _name = '备份我的豆列'

    def run(self):
        pass


ALL_TASKS = OrderedDict([(cls._name, cls) for cls in [
    FollowingFollowerTask,
    BroadcastTask,
    BookTask,
    MovieTask,
    MusicTask,
    BroadcastCommentTask,
    #PhotoAlbumTask,
    #ReviewTask,
    #DoulistTask,
]])
