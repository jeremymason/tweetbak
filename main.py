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

import twitter
from appengine_utilities import sessions
from appengine_utilities.flash import Flash

from google.appengine.ext.webapp import template
from google.appengine.api import users
from google.appengine.ext import webapp
from google.appengine.ext.webapp.util import run_wsgi_app
from google.appengine.ext import db

# Non source control configuration is in localsettings
from localsettings import *

# -- Globals -------------------------------------------------------------
session = sessions.Session()

# -- Models --------------------------------------------------------------
class Configuration(db.Model):
    """Configuration items about this user"""

    twitteruser = db.StringProperty(default = "")
    lastupdated = db.DateTimeProperty(default = None)
    lasttweetid = db.IntegerProperty(default = 1)
    twitter_tweetcount = db.IntegerProperty(default = 0)
    owner = db.UserProperty(required = True)


class Tweet(db.Model):
    """One twitter entry"""

    twitterid = db.IntegerProperty(required = True)
    content = db.StringProperty(required = True, multiline = True)
    date = db.DateTimeProperty(auto_now_add = True)
    owner = db.UserProperty(required = True)


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

def old():
    """
    Function to define how old the last status lock must be
    before refreshing the feed
    """
    return datetime.datetime.now()-datetime.timedelta(minutes=1)
    return datetime.datetime.now()-datetime.timedelta(hours=1)

class Tweets(webapp.RequestHandler):
    """Paginated collection of tweets"""

    def get(self):

        if not users.get_current_user():
            self.redirect("/")

        user = users.get_current_user()
        config = get_config()

        if not config.lastupdated or config.lastupdated < old():
            self.redirect("/refresh")

        tweets = Tweet.all().filter('owner =', user).order('date').fetch(10)
        tweetcount = Tweet.gql('WHERE owner = :user', user=user).count()

        kwargs = {
            'twitteruser': config.twitteruser,
            'tweets': tweets,
            'tweetcount':tweetcount,
            'user': user,
            'url': users.create_logout_url(self.request.uri),
            'url_linktext': 'Logout',
            'request': self.request,
            'flash': Flash(),
            'lastupdated': config.lastupdated,
            'year': datetime.datetime.now().year
            }

        self.response.out.write(template.render('index.html', kwargs))


class Refresh(webapp.RequestHandler):
    """Check the twitter archive for recent posts and archive
    if any are found"""

    def get(self):
        flash = Flash()

        if not users.get_current_user():
            self.redirect("/")

        user = users.get_current_user()

        config = get_config()

        if not config.twitteruser:
            self.redirect("/configure")

        if config.lastupdated and (config.lastupdated > old()):

            flash = Flash()
            flash.msg = "Twitter stream not refreshed -- Recently done ("+str(config.lastupdated)+")"

        else:

            api = twitter.Api(cache = None)
            
            # if this is the first time we're seeing this user
            # do a full refresh
            statuses = api.GetUserTimeline(config.twitteruser, count = 1)
            if config.lasttweetid == 1:
                flash.msg = "Completed a full refresh"
                pages = (int(float(int(statuses[0].user.statuses_count))/float(80)))+1
                i = 1
                largestid = 0
                while i <= pages:
                    statuses = api.GetUserTimeline(
                        config.twitteruser, 
                        page = i
                        )

                    if len(statuses) > 1:
                        for status in statuses:
                            t = Tweet.all().filter("tweetid=", status.id).filter("owner=", user).fetch(1)
                            if len(t) < 1:
                                tweet = Tweet(
                                    twitterid=int(status.id),
                                    content= status.text,
                                    date= datetime.datetime.strptime(
                                        status.created_at, 
                                        '%a %b %d %H:%M:%S +0000 %Y'
                                        ),
                                    owner= user
                                    )

                                tweet.put()

                            if int(status.id) > largestid:
                                largestid = int(status.id)
                    i = i + 1
            else:
                statuses = api.GetUserTimeline(
                    config.twitteruser, 
                    count = 200, 
                    since_id = config.lasttweetid
                    )

                largestid = 0
            
                if len(statuses) < 1:
                    self.redirect("/tweets")

                for status in statuses:
                    t = Tweet.all().filter("tweetid=", status.id).filter("owner=", user).fetch(1)
                    if len(t) < 1:
                        tweet = Tweet(
                            twitterid=int(status.id),
                            content= status.text,
                            date= datetime.datetime.strptime(
                                status.created_at, 
                                '%a %b %d %H:%M:%S +0000 %Y'
                                ),
                            owner= user
                            )

                        tweet.put()

                    if int(status.id) > largestid:
                        largestid = int(status.id)

            # upate config values
            if largestid > 0: config.lasttweetid = largestid
            config.lastupdated = datetime.datetime.now()
            config.twitter_tweetcount = int(statuses[0].user.statuses_count)
            config.put()
            
            # notice to the user
            flash = Flash()
            flash.msg = "Twitter stream refreshed"

        self.redirect('/tweets')


class Configure(webapp.RequestHandler):
    """Configure which twitter account to archive"""
    
    def get(self):
        user = users.get_current_user()
        if not user:
            self.redirect("/")

        config = get_config()

        kwargs = {
            'user': user,
            'url': users.create_logout_url(self.request.uri),
            'url_linktext': 'Logout',
            'twitteruser': config.twitteruser,
            'year': datetime.datetime.now().year
            }

        self.response.out.write(
            template.render('configure.html', kwargs))

    def post(self):
        """update/save the twitter username"""

        user = users.get_current_user()
        if not user: self.redirect("/")

        config = get_config()
        config.twitteruser = self.request.get('twitteruser')
        config.lastupdated = None
        config.lasttweetid = 1
        config.put()

        flash = Flash()
        flash.msg = "Tweetbak configuration updated, all previous tweets removed"

        for t in Tweet.all().filter("owner =", user):
            t.delete()

        self.redirect("/tweets")
        
def get_config():
    """Get the configuration object for the current user"""
    user = users.get_current_user()
    if not user: return False

    config = Configuration.all().filter("owner =", user).fetch(1)

    if not config:
        config = Configuration(owner=user)
        config.lasttweetid = 1
        config.put()
    else:
        config = config[0]

    return config

# -- The main GAE application and routes ---------------------------------
application = webapp.WSGIApplication([
    ('/', Welcome),
    ('/tweets', Tweets),
    ('/refresh', Refresh),
    ('/configure', Configure),
    ], debug=True)


# -- The main function is cached on GAE ----------------------------------
def main():
    run_wsgi_app(application)


# -- DO IT! --------------------------------------------------------------
if __name__ == "__main__":
    main()

