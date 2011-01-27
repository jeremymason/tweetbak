"""
Twitter backup for Google App Engine.

Use this app to archive a twitter stream to GAE.  The twitter API currently
only archives up to 3200 historical tweets(!). Use this to go back further
than that.
"""

# -- Imports -------------------------------------------------------------
import cgi
import datetime
import logging
from math import ceil
import os
import time
import twitter

import simplejson as json
from appengine_utilities import sessions
from appengine_utilities.flash import Flash

from google.appengine.api import taskqueue
from google.appengine.api import users
from google.appengine.ext import db
from google.appengine.ext import webapp
from google.appengine.ext.webapp import template
from google.appengine.ext.webapp.util import run_wsgi_app

# Non source control configuration is in localsettings
from localsettings import *

# -- Globals -------------------------------------------------------------
session = sessions.Session()
MAX_TWEETS_PER_PAGE = 200
TWITTER_CALL_DELAY = 15

# -- Models --------------------------------------------------------------
class TweetStream(db.Model):
    """One twitter users archived stream"""

    twitterid = db.IntegerProperty()
    twitteruser = db.StringProperty()
    count = db.IntegerProperty(default=0)
    lastupdated = db.DateTimeProperty(auto_now_add=True)
    owner = db.UserProperty(required=True)


class Tweet(db.Model):
    """Represents one tweet"""
    
    tweetstream = db.ReferenceProperty(TweetStream, required=True)
    tweetid = db.StringProperty()
    content = db.StringProperty(multiline=True)
    created = db.DateTimeProperty()
    owner = db.UserProperty(required=True)
    

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

        user = users.get_current_user()

        if not user: 
            self.redirect("/")
            return

        tsid = self.request.get('tsid') and self.request.get('tsid') or None
        page = self.request.get('page') and self.request.get('page') or 1
        limit = self.request.get('limit') and self.request.get('limit') or 50
        order = self.request.get('order') and self.request.get('order') or '-created'
        (page, limit) = (int(page), int(limit))
        offset = (page-1)*limit

        tweetstream = get_tweetstream(self.request.get('tsid'))
        
        if not tweetstream:
            self.redirect("/configure")
            return

        tsid = tweetstream.key()
        tweetcount = 0
        twitteruser = ""
        lastupdated = ""
        tweets = []

        if tweetstream:
            tweetcount = tweetstream.count
            twitteruser = tweetstream.twitteruser
            lastupdated = tweetstream.lastupdated
            tweets = Tweet.all(
                ).filter('tweetstream =', tweetstream
                ).filter('owner =', user
                ).order(order
                ).fetch(limit, offset)
        
        kwargs = {
            'twitteruser': twitteruser,
            'twitterstreams': TweetStream.all().filter("owner =", user),
            'tweetcount':tweetcount,
            'tweets': tweets,
            'tsid': tsid,
            'start': offset+1,
            'end': (offset+limit < tweetcount) and offset+limit or tweetcount,
            'prevpage': (page-1 > 0) and (page-1) or None,
            'nextpage': (((page+1)*limit) < tweetcount+limit) and (page+1) or None,
            'limit': limit,
            'user': user,
            'url': users.create_logout_url(self.request.uri),
            'request': self.request,
            'flash': Flash(),
            'lastupdated': lastupdated,
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
        tsid = self.request.get("tsid")

        if not user: 
            self.redirect("/")

        if not tsid:
            flash.msg += "I couldn't find the correct twitter stream to refresh. Sorry."
            self.redirect("/tweets")

        # Grab the twitter stream
        tweetstream = get_tweetstream(tsid)

        # Start on the first page of tweets
        page = 1
        pages = ceil(tweetstream.count/MAX_TWEETS_PER_PAGE)+1
        
        for i in xrange(1, int(pages)+1):
            taskqueue.add(url = "/tweetretreiver", 
                queue_name = "get-tweets",
                name = "GetTweets-"+tweetstream.twitteruser+"-"+str(i)+"-"+str(int(time.time())),
                countdown = TWITTER_CALL_DELAY * i,
                params = {
                    'page': i,
                    'tsid': tweetstream.key()
                },
                )
            logging.debug('Enqueued get-tweets for page'+str(i)+" start at tweet "+str(MAX_TWEETS_PER_PAGE*i))

        # notice to the user
        flash.msg += "Twitter stream queued for archive. "+str(pages)+" operations required, this could take a few minutes."

        self.redirect('/tweets?tsid='+tsid)
        
        
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

        tweetstreams = [x for x in TweetStream.all().filter('owner =', user)]

        kwargs = {
            'user': user,
            'url': users.create_logout_url(self.request.uri),
            'tweetstreams': tweetstreams,
            'year': datetime.datetime.now().year
            }

        self.response.out.write(
            template.render('configure.html', kwargs))

    def post(self):
        """update/save/delete twitter stream archives"""

        user = users.get_current_user()
        if not user: 
            self.redirect("/")

        if self.request.get("action") == "delete":
            # Delete all existing backedup tweets
            tweetstream = TweetStream.get(self.request.get("tsid"))
            tweets = Tweet.all().filter("tweetstream=",tweetstream).filter("owner=", user)
            for tweet in tweets:
                tweet.delete()
            tweetstream.delete()

        elif self.request.get("action") == "add":
            twitteruser = self.request.get("twitteruser")
            new_tweetstream(twitteruser = twitteruser)

        flash = Flash()
        flash.msg = "Tweetbak configuration updated"

        self.redirect("/tweets")

def new_tweetstream(twitteruser = ""):
    """create a new tweetstream for a given username"""

    user = users.get_current_user()
    if not user: 
        return None
    
    tweetstream = None

    # Create the tweetstream only if it doesn't exist already and we can find the 
    # twitter user
    api = twitter.Api(cache = None)

    # A few global updates to the tweetstream
    statuses = api.GetUserTimeline(twitteruser, count = 1)

    if len(statuses) < 1:
        flash = Flash()
        flash.msg = "User not found"
        return None

    status = statuses[0]
    if not status:
        return None
    
    if TweetStream.all().filter('owner =', user).filter('twitteruser =', twitteruser).count() < 1:
        tweetstream = TweetStream(owner = user, twitteruser = twitteruser)
        tweetstream.twitterid = status.user.id
        tweetstream.count = status.user.statuses_count
        tweetstream.put()
    else:
        tweetstream = TweetStream.all().filter('owner =', user).filter('twitteruser =', twitteruser).get()

    return tweetstream

def get_tweetstream(tsid = None):
    """Get a tweetstream object for the current user
    If tsid is a key object for a GAE model
    If tsid is not supplied, use the first found tweetstream
    If a tweetstream isn't found, create an empty one and return that
    """
    
    user = users.get_current_user()
    if not user: 
        return None

    if tsid:
        tweetstream = TweetStream.get(tsid)

    elif TweetStream.all().count() > 0:
        tweetstream = TweetStream.all().filter('owner =', user).fetch(1)
        if len(tweetstream) > 0:
            tweetstream = tweetstream[0]
    else:
        tweetstream = None

    return tweetstream


class Retreiver(webapp.RequestHandler):
    """Retrieve a batch of tweets and create tweet objects for them"""

    def post(self):
        logging.debug("Start Retreiver... entering webhook")

        # fail if the tweetstream is not supplied ofr not found
        if not self.request.get("tsid"): return
        tweetstream = TweetStream.get(self.request.get("tsid"))
        if not tweetstream: return

        logging.debug("Getting tweets for "+tweetstream.twitteruser)

        # fail if the page is not supplied
        if not self.request.get("page"): return
        page = self.request.get("page")

        api = twitter.Api(cache = None)

        logging.debug("Got API ")

        # Get a couple hundred tweets
        statuses = api.GetUserTimeline(
            tweetstream.twitteruser, 
            page = page,
            trim_user = True,
            include_rts = True,
            count = MAX_TWEETS_PER_PAGE
            )

        logging.debug("Got statuses: "+str(len(statuses)))

        for status in statuses:
            # Don't save statuses we have already saved
            if not Tweet.all().filter("tweetid =", str(status.id)).get():
                try:
                    tweet = Tweet(tweetstream = tweetstream, owner = tweetstream.owner)
                    tweet.tweetid = str(status.id)
                    tweet.content = status.text
                    tweet.created = datetime.datetime.strptime(
                        status.created_at, 
                        '%a %b %d %H:%M:%S +0000 %Y'
                        )
                    tweet.put()
                except:
                    logging.debug("Error saving status: "+status.text)
        logging.debug("Done retreiver... exiting webhook")

class Deleter(webapp.RequestHandler):
    
    def get(self):
        taskqueue.add(url = "/tweetdeleter")
        logging.debug('Added delete all tweets task to the default queue')

    def post(self):
        logging.debug("Start deleter... entering webhook")
        tweets = Tweet.all()
        for t in tweets:
            t.delete()
        logging.debug("Done deleter... exiting webhook")

        
        
# -- The main GAE application and routes ---------------------------------
application = webapp.WSGIApplication([
    ('/', Welcome),
    ('/tweets', Tweets),
    ('/refresh', Refresh),
    ('/configure', Configure),
    ('/tweetretreiver', Retreiver),
    ('/tweetdeleter', Deleter),
    ], debug=True)


# -- The main function is cached on GAE ----------------------------------
def main():
    run_wsgi_app(application)


# -- DO IT! --------------------------------------------------------------
if __name__ == "__main__":
    main()

