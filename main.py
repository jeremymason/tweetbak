import datetime
import cgi
import os

import simplejson
from google.appengine.ext.webapp import template
from google.appengine.api import users
from google.appengine.ext import webapp
from google.appengine.ext.webapp.util import run_wsgi_app
from google.appengine.ext import db

# == models =======================================================
class Tweet(db.Model):
    content = db.StringProperty(required = True)
    owner = db.UserProperty(required = True)
    date = db.DateTimeProperty(auto_now_add=True)


class TwitterConfig(db.Model):
    content = db.StringProperty(required = True)
    owner = db.UserProperty(required = True)
    date = db.DateTimeProperty(auto_now_add=True)

# == controllers =================================================
class Welcome(webapp.RequestHandler):

    def get(self):
        if users.get_current_user():
            self.redirect("/tweets", False)
        login_url = users.create_login_url(self.request.uri)
        self.response.out.write(template.render('welcome.html', {'login_url':login_url}))


class Tweets(webapp.RequestHandler):

    def get(self):
        if not users.get_current_user():
            self.redirect("/")

        q = Tweets.all().order('date')
        tweets = q.fetch(10)

        template_values = {
            'tweets': tweets,
            'user': users.get_current_user(),
            'url': users.create_logout_url(self.request.uri),
            'url_linktext': 'Logout',
            'request': self.request,
            'year': datetime.datetime.now().year
            }

        self.response.out.write(template.render('index.html', template_values))

    def post(self):
        tweet = Tweet(content = self.request.get('content'),
            owner = users.get_current_user(),
            )
        tweet.put()
        self.redirect('/tweets')

class Configure(webapp.RequestHandler):
    
    def get(self):
        if not users.get_current_user():
            self.redirect("/")

        template_values = {
            'user': users.get_current_user(),
            'url': users.create_logout_url(self.request.uri),
            'url_linktext': 'Logout',
            'request': self.request,
            'year': datetime.datetime.now().year
            }
            #TODO: create configure page
        self.response.out.write(template.render('configure.html', template_values))

application = webapp.WSGIApplication(
                                     [
                                     ('/', Welcome),
                                     ('/tweets', Tweets)
                                     ],
                                     debug=True)
def main():
    run_wsgi_app(application)

if __name__ == "__main__":
    main()

