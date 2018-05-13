# encoding: utf-8
import re
from ..handlers import BaseRequestHandler
from db import Account

class Login(BaseRequestHandler):
    """
    登录豆瓣
    """

    def get(self):
        self.render('settings/accounts/login.html')


class Index(BaseRequestHandler):
    """
    管理帐号
    """

    def get(self):
        self.render('settings/accounts/index.html', rows=Account.select())


class Add(BaseRequestHandler):
    """
    添加帐号
    """

    def post(self):
        session = self.get_argument('session')
        homepage = self.get_argument('homepage')
        
        try:
            name = re.findall(r'^https://www\.douban\.com/people/(.+)/$', homepage).pop()
            account = Account.get(Account.name == name)
            account.session = session
            account.save()
        except Account.DoesNotExist:
            Account.create(session=session, name=name)
        except IndexError:
            pass

        self.write('OK')
        
        
