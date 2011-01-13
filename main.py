"""
Twitter backup for Google App Engine.

Use this app to archive a twitter stream to GAE.  The twitter API currently
only archives up to 3200 historical tweets(!). Use this to go back further
than that.
"""

# -- Imports -------------------------------------------------------------
import datetime
import cgi
import os

import tweepy

from google.appengine.ext.webapp import template
from google.appengine.api import users
from google.appengine.ext import webapp
from google.appengine.ext.webapp.util import run_wsgi_app
from google.appengine.ext import db

# Non source control configuration is in localsettings
from localsettings import *

# -- Constants -----------------------------------------------------------
REQUEST_TOKEN_URL = "https://api.twitter.com/oauth/request_token"
ACCESS_TOKEN_URL = "https://api.twitter.com/oauth/access_token"
AUTHORIZE_URL = "https://api.twitter.com/oauth/authorize"


# -- Models --------------------------------------------------------------
class Tweet(db.Model):
    """One twitter entry"""

    content = db.StringProperty(required = True)
    date = db.DateTimeProperty(auto_now_add=True)
    owner = db.UserProperty(required = True)


# -- Twitter db entries --------------------------------------------------


# -- Controllers ---------------------------------------------------------
class Welcome(webapp.RequestHandler):
    """The welcome page, information and login"""

    def get(self):

        if users.get_current_user():
            self.redirect("/tweets", False)

        kwargs = {
            'login_url': users.create_login_url(self.request.uri)
            }

        self.response.out.write(template.render('welcome.html', kwargs))


class Tweets(webapp.RequestHandler):
    """Paginated collection of tweets"""

    def get(self):

        if not users.get_current_user():
            self.redirect("/")

        tweets = Tweet.all().order('date').fetch(10)

        kwargs = {
            'tweets': tweets,
            'user': users.get_current_user(),
            'url': users.create_logout_url(self.request.uri),
            'url_linktext': 'Logout',
            'request': self.request,
            'year': datetime.datetime.now().year
            }

        self.response.out.write(template.render('index.html', kwargs))

    def refresh(self):

        tweet = Tweet(
            content = self.request.get('content'),
            owner = users.get_current_user(),
            )

        tweet.put()

        self.redirect('/tweets')


class Configure(webapp.RequestHandler):
    """Configure which twitter account to archive"""
    
    def get(self):

        session = appengine_utilities.sessions.Session()

        if not users.get_current_user():
            self.redirect("/")

        auth = tweepy.OAuthHandler(
            TWEETBAK_CONSUMER_KEY, 
            TWEETBAK_CONSUMER_SECRET)

        redirect_url = auth.get_authorization_url()
        
        
        kwargs = {
            'user': users.get_current_user(),
            'url': users.create_logout_url(self.request.uri),
            'url_linktext': 'Logout',
            'request': self.request,
            'year': datetime.datetime.now().year
            }

        #TODO: create configure html page

        self.response.out.write(
            template.render('configure.html', kwargs))

# -- The main GAE application and routes ---------------------------------
application = webapp.WSGIApplication([
    ('/', Welcome),
    ('/tweets', Tweets),
    ('/refresh', Tweets.refresh),
    ('/configure', Configure),
    ], debug=True)


# -- The main function is cached on GAE ----------------------------------
def main():
    run_wsgi_app(application)


# -- DO IT! --------------------------------------------------------------
if __name__ == "__main__":
    main()

