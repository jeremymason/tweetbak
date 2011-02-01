"""
Twitter backup for Google App Engine.

Use this app to archive a twitter stream to GAE.  The twitter API currently
only archives up to 3200 historical tweets(!). Use this to go back further
than that.
"""






# -- Imports -------------------------------------------------------------
import csv
import cStringIO
import datetime
import logging
from math import ceil
import random
import time
import twitter

import simplejson as json
from appengine_utilities import sessions
from appengine_utilities.flash import Flash

from google.appengine.api import mail
from google.appengine.api import memcache
from google.appengine.api import taskqueue
from google.appengine.api import users
from google.appengine.ext import db
from google.appengine.ext import search
from google.appengine.ext import webapp
from google.appengine.ext.webapp import template
from google.appengine.ext.webapp.util import run_wsgi_app

# Non source control configuration is in localsettings
from localsettings import *

# -- Globals -------------------------------------------------------------
session = sessions.Session()
MAX_TWEETS_PER_PAGE = 200
TWITTER_CALL_DELAY = 1


# -- http://code.google.com/appengine/articles/sharding_counters.html ----
class GeneralCounterShardConfig(db.Model):
    """Tracks the number of shards for each named counter."""
    name = db.StringProperty(required=True)
    num_shards = db.IntegerProperty(required=True, default=20)


class GeneralCounterShard(db.Model):
    """Shards for each named counter"""
    name = db.StringProperty(required=True)
    count = db.IntegerProperty(required=True, default=0)


def get_count(name):
    """Retrieve the value for a given sharded counter.

    Parameters:
      name - The name of the counter
    """
    total = memcache.get(name)
    if total is None:
        total = 0
        for counter in GeneralCounterShard.all().filter('name = ', name):
            total += counter.count
        memcache.add(name, str(total), 60)
    return total


def increment(name):
    """Increment the value for a given sharded counter.

    Parameters:
      name - The name of the counter
    """
    config = GeneralCounterShardConfig.get_or_insert(name, name=name)
    def txn():
        index = random.randint(0, config.num_shards - 1)
        shard_name = name + str(index)
        counter = GeneralCounterShard.get_by_key_name(shard_name)
        if counter is None:
            counter = GeneralCounterShard(key_name=shard_name, name=name)
        counter.count += 1
        counter.put()
    db.run_in_transaction(txn)
    memcache.incr(name)
# -- http://code.google.com/appengine/articles/sharding_counters.html ----


# -- Models --------------------------------------------------------------
class TweetStream(db.Model):
    """One twitter users archived stream"""

    twitterid = db.IntegerProperty()
    twitteruser = db.StringProperty()
    raw = db.TextProperty()
    count = db.IntegerProperty(default = 0)
    lastupdated = db.DateTimeProperty(auto_now_add = True)
    owner = db.UserProperty(required = True)


class Tweet(search.SearchableModel):
    """Represents one tweet"""
    
    tweetstream = db.ReferenceProperty(TweetStream, required = True)
    tweetid = db.StringProperty()
    content = db.StringProperty(multiline = True)
    raw = db.TextProperty()
    created = db.DateTimeProperty()
    owner = db.UserProperty(required = True)

    @classmethod
    def SearchableProperties(cls):
        return [['content']]    


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
        tweetcount = "no"
        twitteruser = ""
        lastupdated = ""
        tweets = []

        if tweetstream:
            tscount = tweetstream.count
            twitteruser = tweetstream.twitteruser
            lastupdated = tweetstream.lastupdated
            tweets = Tweet.all(
                ).filter('tweetstream =', tweetstream
                ).filter('owner =', user
                ).order(order
                ).fetch(limit, offset)

            countername = str(tweetstream.owner)+"-"+str(tweetstream.twitterid)+"-"
            tweetcount = int(get_count(countername))
        
        kwargs = {
            'twitteruser': twitteruser,
            'twitterstreams': TweetStream.all().filter("owner =", user),
            'tweetcount':tweetcount,
            'tscount':tscount,
            'tweets': tweets,
            'tsid': tsid,
            'start': offset+1,
            'i':1,
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


class Search(webapp.RequestHandler):
    """Search archived tweets"""

    def get(self):
        flash = Flash()

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

        terms = self.request.get('term')

        if not terms:
            flash.msg = "Search term not found"
            self.redirect("/tweets")
            return
            
        tsid = tweetstream.key()
        tweetcount = "no"
        resultcount = 0
        twitteruser = ""
        lastupdated = ""
        tweets = []

        if tweetstream:
            tscount = tweetstream.count
            twitteruser = tweetstream.twitteruser
            lastupdated = tweetstream.lastupdated
            tweets = Tweet.all(
                ).filter('tweetstream =', tweetstream
                ).filter('owner =', user
                ).search(terms
                ).order(order
                ).fetch(limit, offset)
            resultcount = Tweet.all(
                ).filter('tweetstream =', tweetstream
                ).filter('owner =', user
                ).search(terms
                ).order(order
                ).count()
            tweetcount = Tweet.all(
                ).filter('tweetstream =', tweetstream
                ).filter('owner =', user
                ).order(order
                ).count()

        s = resultcount != 1 and "s" or ""
        results = "".join(["Your search for: <b>", terms, "</b> returned ", str(resultcount), " result", s, "."])
        
        kwargs = {
            'twitteruser': twitteruser,
            'twitterstreams': TweetStream.all().filter("owner =", user),
            'tweetcount':resultcount,
            'tscount':tscount,
            'tweets': tweets,
            'tsid': tsid,
            'start': tweets and offset+1 or 0,
            'end': (offset+limit < resultcount) and offset+limit or resultcount,
            'prevpage': (page-1 > 0) and (page-1) or None,
            'nextpage': (((page+1)*limit) < resultcount+limit) and (page+1) or None,
            'limit': limit,
            'user': user,
            'terms':terms,
            'request': self.request,
            'flash': Flash(),
            'results': results,
            'lastupdated': lastupdated,
            'year': datetime.datetime.now().year
            }

        self.response.out.write(template.render('index.html', kwargs))


class Configure(webapp.RequestHandler):
    """Configure which twitter account to archive"""

    def get(self):
        user = users.get_current_user()
        if not user: 
            self.redirect("/")

        tweetstreams = (x for x in TweetStream.all().filter('owner =', user))

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

            # Delete all existing archived tweets and associated support classes
            tweetstream = TweetStream.get(self.request.get("tsid"))

            taskqueue.add(
                url = "/tweetdeleter", 
                name = "DeleteAllTweets-"+tweetstream+"-"+str(int(time.time())),
                params = {
                    'user': user.user_id(),
                    'tsid': tweetstream
                    }
                )

        elif self.request.get("action") == "add":
            twitteruser = self.request.get("twitteruser")
            tweetstream = new_tweetstream(twitteruser = twitteruser)
            self.redirect("/refresh?tsid="+str(tweetstream.key()))
            return

        flash = Flash()
        flash.msg = "Tweetbak configuration updated"

        self.redirect("/tweets")

def new_tweetstream(twitteruser = ""):
    """create a new tweetstream for a given username"""

    user = users.get_current_user()
    if not user: 
        return None
    
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
    
    key_name = user.email()+"-"+twitteruser

    tweetstream = TweetStream.get_or_insert(
        owner = user,
        twitteruser = twitteruser,
        key_name = key_name
        )
    tweetstream.twitterid = status.user.id
    tweetstream.raw = str(status)
    tweetstream.count = status.user.statuses_count
    tweetstream.put()

    return tweetstream

def get_tweetstream(tsid = None):
    """Get a tweetstream object for the current user
    If tsid is a key object for a GAE model
    If tsid is not supplied, use the first found tweetstream
    If a tweetstream isn't found, just return None
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

        twitteruser = tweetstream.twitteruser
        
        logging.debug("Getting tweets for "+twitteruser)

        # fail if the page is not supplied
        if not self.request.get("page"): return
        page = self.request.get("page")

        api = twitter.Api(cache = None)

        logging.debug("Got API ")

        # Update the tweetstream statistics
        statuses = api.GetUserTimeline(twitteruser, count = 1)
        status = statuses[0]
        if not status:
            logging.debug("Error getting tweets for "+twitteruser)
            return

        # update the vaules and save them
        tweetstream.raw = str(status)
        tweetstream.count = status.user.statuses_count
        tweetstream.put()

        # Get a couple hundred tweets
        statuses = api.GetUserTimeline(
            twitteruser, 
            page = page,
            trim_user = True,
            include_rts = True,
            count = MAX_TWEETS_PER_PAGE
            )

        logging.debug("Got statuses: "+str(len(statuses)))

        for status in statuses:

            # Don't save statuses we've already saved
            key_name = str(tweetstream.key())+"-"+str(status.id)

            if not Tweet.get_by_key_name(key_name):
                try:
                    tweet = Tweet(tweetstream = tweetstream, owner = tweetstream.owner, key_name = key_name)
                    tweet.tweetid = str(status.id)
                    tweet.content = status.text
                    tweet.raw = str(status)
                    tweet.created = datetime.datetime.strptime(
                        status.created_at, 
                        '%a %b %d %H:%M:%S +0000 %Y'
                        )
                    tweet.put()

                    # http://code.google.com/appengine/articles/sharding_counters.html
                    # use a sharded counter instead of .count()
                    countername = str(tweetstream.owner)+"-"+str(tweetstream.twitterid)+"-"
                    increment(countername)

                except:
                    logging.debug("Error saving status: "+status.text)
        logging.debug("Done retreiver... exiting webhook")

class Deleter(webapp.RequestHandler):
    
    def get(self):

        user = users.get_current_user()
        if not user: 
            return None
        
        taskqueue.add(
            url = "/tweetdeleter", 
            name = "DeleteAllTweets-"+user.user_id()+"-"+str(int(time.time())),
            params = {
                'user': user.user_id()
                }
            )
        
        logging.debug('Enqueued task to delete all tweets')

    def post(self):
        logging.debug("Start deleter... entering webhook")

        user = users.get_current_user()
        if not user or user.user_id() != self.request.get("user"):
            return None

        tsid = self.request.get("user")

        if tsid:
            tweetstreams = TweetStream.all().filter("owner =", user).filter("tweetstream =", tsid)
        else:
            tweetstreams = TweetStream.all().filter("owner =", user)

        # Remove all tweetstreams indicated for this user (cascade to the sharded counter entities)
        for t in tweetstreams:

            countername = str(tweetstream.owner)+"-"+str(tweetstream.twitterid)+"-"

            for gcc in GeneralCounterShardConfig.get_by_key_name(countername):
                gcc.delete()

            for gc in GeneralCounterShard.get_by_key_name(countername):
                gc.delete()

            # Remove all tweet entries for this user
            for t in Tweet.all().filter("tweetstream =", t):
                t.delete()

            t.delete()

        logging.debug("Done deleter... exiting webhook")

        
class Exporter(webapp.RequestHandler):
    """Send the exported tweet list to your gmail acct"""

    def get(self):

        flash = Flash()
        user = users.get_current_user()

        if not user: 
            self.redirect("/")
            return

        logging.debug('found user')

        tsid = self.request.get('tsid') and self.request.get('tsid') or None
        tweetstream = get_tweetstream(self.request.get('tsid'))

        flash.msg = "Your export request has been queued.  You should receive an email with "+tweetstream.twitteruser+"'s tweets shortly."

        if not tweetstream:
            flash.msg = "Could not find tweetstream."
            redir = "/tweets"
            if tsid: redir += "?tsid="+tsid
            self.redirect(redir)
            return

        logging.debug('found tweetstream, enqueuing')
        taskqueue.add(url = "/export", 
            name = "ExportTweets-"+tweetstream.twitteruser+"-"+str(int(time.time())),
            countdown = TWITTER_CALL_DELAY,
            params = {
                'tsid': tweetstream.key()
                },
            )

        logging.debug('Added export all tweets task to the default queue')

        redir = "/tweets"
        if tsid: redir += "?tsid="+tsid
        
        logging.debug("---"+str(flash.msg)+"---")
        self.redirect(redir)

    def post(self):
        logging.debug("Start exporter...")
        
        tsid = self.request.get('tsid')

        if tsid:
            tweetstream = TweetStream.get(tsid)

        if tweetstream:

            logging.debug('Exporting tweets for ', tweetstream.twitteruser)

            # File like object to gather the exported data
            out = cStringIO.StringIO() 
            export = csv.writer(out, dialect = 'excel')
            filename = tweetstream.twitteruser+"-"+str(int(time.time()))+".csv"

            tweets = Tweet.all(
                ).filter('tweetstream =', tweetstream
                ).order('-created')
            data = ([x.created, x.content.encode('utf-8') ] for x in tweets)
            export.writerows(data)

            # send email to the owner of the tweetstream with the exported data
            # attached
            mail.send_mail(
                sender = 'jeremycmason@gmail.com', 
                to = str(tweetstream.owner.email()), 
                subject = 'Twitter archive from Tweetbak', 
                body = 'Your twitter archive is attached',
                attachments=[(filename, out.getvalue())]
                ) 

        else:

            logging.debug("Could not get tweetstream for ", self.request.get('tsid'))

        logging.debug("Done exporter...")
        
# -- The main GAE application and routes ---------------------------------
application = webapp.WSGIApplication([
    ('/', Welcome),
    ('/tweets', Tweets),
    ('/search', Search),
    ('/refresh', Refresh),
    ('/configure', Configure),
    ('/export', Exporter),
    ('/tweetretreiver', Retreiver),
    ('/tweetdeleter', Deleter),
    ], debug=True)


# -- The main function is cached on GAE ----------------------------------
def main():
    run_wsgi_app(application)


# -- DO IT! --------------------------------------------------------------
if __name__ == "__main__":
    main()

