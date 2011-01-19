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
from math import ceil

import twitter
import simplejson as json
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


class TweetStream(db.Model):
    """One twitter users archived stream"""

    twitterid = db.IntegerProperty()
    twitteruser = db.StringProperty()
    tweetcount = db.IntegerProperty(default = 0)
    tweetids = db.ListProperty(long) # Cached list of tweet IDs
    tweets = db.StringListProperty()
    lastupdated = db.DateTimeProperty(auto_now_add = True)
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
    return datetime.datetime.now()-datetime.timedelta(hours=1)

class Tweets(webapp.RequestHandler):
    """Paginated collection of tweets"""

    def get(self):

        user = users.get_current_user()

        if not user: 
            self.redirect("/")

        page = self.request.get('page') and self.request.get('page') or 1
        limit = self.request.get('limit') and self.request.get('limit') or 10
        order = self.request.get('order') and self.request.get('order') or '-date'
        (page, limit) = (int(page), int(limit))
        offset = (page-1)*limit

        config = get_config()

        if not config or not config.twitteruser: 
            self.redirect("/configure")

        tweetstream = get_tweetstream()
        
        tweetcount = tweetstream.tweetids and len(tweetstream.tweetids) or 0
        tweets = [json.loads(x) for x in tweetstream.tweets[offset:offset+limit]]

        kwargs = {
            'twitteruser': config.twitteruser,
            'tweetcount':tweetcount,
            'tweets': tweets,
            'start': offset+1,
            'end': (offset+limit < tweetcount) and offset+limit or tweetcount,
            'prevpage': (page-1 > 0) and (page-1) or None,
            'nextpage': (((page+1)*limit) < tweetcount+limit) and (page+1) or None,
            'limit': limit,
            'user': user,
            'url': users.create_logout_url(self.request.uri),
            'request': self.request,
            'flash': Flash(),
            'lastupdated': tweetstream.lastupdated,
            'year': datetime.datetime.now().year
            }

        self.response.out.write(template.render('index.html', kwargs))


class Refresh(webapp.RequestHandler):
    """Check the twitter archive for recent posts and archive
    if any are found"""

    def get(self):
        flash = Flash()
        flash.msg = ""

        user = users.get_current_user()

        if not user: 
            self.redirect("/")

        config = get_config()

        if not config or not config.twitteruser: 
            self.redirect("/configure")


        api = twitter.Api(cache = None)

        # A few global updates to the config
        statuses = api.GetUserTimeline(config.twitteruser, count = 1)
        status = statuses[0]

        config.lastupdated = datetime.datetime.now()
        config.twitter_tweetcount = status.user.statuses_count
        total_tweets = status.user

        # Grab the twitter stream
        tweetstream = get_tweetstream()

        # Start on the first page of tweets
        page = 1
        pages = ceil(config.twitter_tweetcount/200.0)

        # Get the first page of tweets to prime the while loop
        statuses = api.GetUserTimeline(
            config.twitteruser, 
            page = page,
            trim_user = True,
            count = 200
            )

        for status in statuses:

            if status.id in tweetstream.tweetids:
                # We've seen this tweet already, don't archive it again
                continue

            # Save this tweet!
            tweet = {
                'id': status.id,
                'content': status.text,
                'date': str(datetime.datetime.strptime(
                    status.created_at, 
                    '%a %b %d %H:%M:%S +0000 %Y'
                    ))
                }

            tweetstream.tweets.append(json.dumps(tweet))
            tweetstream.tweetids.append(long(status.id))

        config.lasttweetid = max(tweetstream.tweetids)

        config.put()
        tweetstream.put()
        
        # notice to the user
        flash.msg += "Twitter stream refreshed"

        self.redirect('/tweets')
        
        
    def post(self):
        '''
        Perform the twitter API call and store the resulting tweet objects
        '''

class Configure(webapp.RequestHandler):
    """Configure which twitter account to archive"""
    
    def get(self):
        user = users.get_current_user()
        if not user: 
            self.redirect("/")

        config = get_config()
        twitteruser = config and config.twitteruser or ""

        kwargs = {
            'user': user,
            'url': users.create_logout_url(self.request.uri),
            'twitteruser': twitteruser,
            'year': datetime.datetime.now().year
            }

        self.response.out.write(
            template.render('configure.html', kwargs))

    def post(self):
        """update/save the twitter username"""

        user = users.get_current_user()
        if not user: 
            self.redirect("/")

        # Delete all existing backedup tweets
        t = TweetStream.all().filter("owner = ", user).filter("twittername", config.twitteruser)

        if t:
            t.delete()

        config = get_config()
        config.twitteruser = self.request.get('twitteruser')
        config.lastupdated = None
        config.lasttweetid = 0
        config.twitter_tweetcount = 0
        config.put()

        flash = Flash()
        flash.msg = "Tweetbak configuration updated, all previous tweets removed"

        self.redirect("/tweets")

        
def get_config():
    """Get the configuration object for the current user"""

    user = users.get_current_user()
    if not user: 
        return None

    config = Configuration.all().filter("owner = ", user).fetch(1)

    if not config:
        config = Configuration(owner = user)
        config.lastupdated = None
        config.lasttweetid = 0
        config.twitter_tweetcount = 0
        config.put()
    else:
        config = config[0]

    return config

def get_tweetstream():
    """Get the tweetstream object for the current user"""

    user = users.get_current_user()
    if not user: 
        return None

    tweetstream = TweetStream.all().filter('owner =', user).fetch(1)
    
    if not tweetstream:
        tweetstream = TweetStream(owner = user, twitteruser = "")
        tweetstream.tweets = []
        tweetstream.twitterid = 0
        tweetstream.tweetids = []
        tweetstream.owner = user
        tweetstream.put()
    else:
        tweetstream = tweetstream[0]

    return tweetstream


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

