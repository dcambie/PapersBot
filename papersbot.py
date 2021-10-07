#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Flowbot
#
# purpose:  read journal RSS feeds and tweet selected entries
# license:  MIT License
# author:   Christopher Gordon Thomson
# e-mail:   christhomson95@hotmail.com
#

import imghdr
import os
import random
import re
import sys
import time
import urllib
import warnings

import yaml

import bs4
import feedparser
import tweepy


# This is the regular expression that selects the papers of interest
regex_include = re.compile(r"""
(   Flow.chemistry
    | continuous.flow
    | flow.synthesis
    | flow.reactor
    | continuous.synthesis
    | flow.conditions
    | \bContinuous\b.*?\bmicroreactor\b.*?
  )
  """, re.IGNORECASE | re.VERBOSE)

# This could be merged with the previous regex, but that would be less readable
regex_exclude = re.compile(r"""
  (   ventricular arrhythmia
    | LVAD
  )
""")

# We select entries based on title or summary (abstract, for some feeds)
def entryMatches(entry):
    # Malformed entry
    if "title" not in entry:
        return False

    if regex_include.search(entry.title):
        return False if regex_exclude.search(entry.title) else True

    if "summary" not in entry:
        return False

    if regex_include.search(entry.summary):
        return False if regex_exclude.search(entry.title) else True


# Find the URL for an image associated with the entry
def findImage(entry):
    if "description" not in entry:
        return

    soup = bs4.BeautifulSoup(entry.description, "html.parser")
    img = soup.find("img")
    if img:
        img = img["src"]
        if len(img) == 0:
            return
        # If address is relative, append root URL
        if img[0] == "/":
            p = urllib.parse.urlparse(entry.id)
            img = f"{p.scheme}://{p.netloc}" + img

    return img


# Convert string from HTML to plain text
def htmlToText(s):
    return bs4.BeautifulSoup(s, "html.parser").get_text()


def downloadImage(url):
    if not url:
        return None

    try:
        img, _ = urllib.request.urlretrieve(url)
    except Exception:
        return None
    ext = imghdr.what(img)
    res = img + "." + ext
    os.rename(img, res)

    # Images smaller than 4 KB have a problem, and Twitter will complain
    if os.path.getsize(res) < 4096:
        os.remove(res)
        return None

    return res


# Connect to Twitter and authenticate
#   Credentials are passed in the environment,
#   or stored in "credentials.yml" which contains four lines:
#   CONSUMER_KEY: "x1F3s..."
#   CONSUMER_SECRET: "3VNg..."
#   ACCESS_KEY: "7109..."
#   ACCESS_SECRET: "AdnA..."
#
def initTwitter():
    if 'CONSUMER_KEY' in os.environ:
        cred = {'CONSUMER_KEY': os.environ['CONSUMER_KEY'],
                'CONSUMER_SECRET': os.environ['CONSUMER_SECRET'],
                'ACCESS_KEY': os.environ['ACCESS_KEY'],
                'ACCESS_SECRET': os.environ['ACCESS_SECRET']}
    else:
        with open("credentials.yml", "r") as f:
            cred = yaml.safe_load(f)

    auth = tweepy.OAuthHandler(cred["CONSUMER_KEY"], cred["CONSUMER_SECRET"])
    auth.set_access_token(cred["ACCESS_KEY"], cred["ACCESS_SECRET"])
    return tweepy.API(auth)


# Read our list of feeds from file
def readFeedsList():
    with open("feeds.txt", "r") as f:
        feeds = [s.partition("#")[0].strip() for s in f]
        return [s for s in feeds if s]


# Remove unwanted text some journals insert into the feeds
def cleanText(s):
    # Annoying ASAP tags
    s = s.replace("[ASAP]", "")
    # Some feeds have LF characters
    s = s.replace("\x0A", "")
    # Remove (arXiv:1903.00279v1 [cond-mat.mtrl-sci])
    s = re.sub(r"\(arXiv:.+\)", "", s)
    # Remove multiple spaces, leading and trailing space
    return re.sub("\\s\\s+", " ", s).strip()


# Read list of feed items already posted
def readPosted():
    try:
        with open("posted.dat", "r") as f:
            return f.read().splitlines()
    except IOError:
        return []


class PapersBot:
    posted = []
    n_seen = 0
    n_tweeted = 0

    def __init__(self, doTweet=True):
        self.feeds = readFeedsList()
        self.posted = readPosted()

        # Read parameters from configuration file
        try:
            with open("config.yml", "r") as f:
                config = yaml.safe_load(f)
        except IOerror as e:
            warnings.warn(f"Exception {e} caught!")
            config = {}
        self.throttle = config.get("throttle", 0)
        self.wait_time = config.get("wait_time", 5)
        self.shuffle_feeds = config.get("shuffle_feeds", True)
        self.url_blacklist = config.get("url_blacklist", [])
        self.url_blacklist = [re.compile(s) for s in self.url_blacklist]

        # Shuffle feeds list
        if self.shuffle_feeds:
            random.shuffle(self.feeds)

        # Connect to Twitter, unless requested not to
        if doTweet:
            self.api = initTwitter()
        else:
            self.api = None

        # Maximum shortened URL length (previously short_url_length_https)
        urllen = 23
        # Maximum URL length for media (previously characters_reserved_per_media)
        imglen = 24
        # Determine maximum tweet length
        self.maxlength = 280 - (urllen + 1) - imglen

        # Start-up banner
        print(f"This is PapersBot running at {time.strftime('%Y-%m-%d %H:%M:%S %Z')}")
        if self.api:
            timeline = self.api.user_timeline(count=1)
            if len(timeline) > 0:
                print(f"Last tweet was posted at {timeline[0].created_at} (UTC)")
            else:
                print(f"No tweets posted yet? Welcome, new user!")
        print(f"Feed list has {len(self.feeds)} feeds\n")

    # Add to tweets posted
    def addToPosted(self, url):
        with open("posted.dat", "a+") as f:
            print(url, file=f)
        self.posted.append(url)

    # Send a tweet for a given feed entry
    def sendTweet(self, entry):
        title = cleanText(htmlToText(entry.title))
        length = self.maxlength

        # Usually the ID is the canonical URL, but not always
        if entry.id[:8] == "https://" or entry.id[:7] == "http://":
            url = entry.id
        else:
            url = entry.link

        # URL may be malformed
        if not (url[:8] == "https://" or url[:7] == "http://"):
            print(f"INVALID URL: {url}\n")
            return

        tweet_body = title[:length] + " " + url

        # URL may match our url_blacklist
        for regexp in self.url_blacklist:
            if regexp.search(url):
                print(f"BLACKLISTED: {tweet_body}\n")
                self.addToPosted(entry.id)
                return

        try:
            if any([term in entry.tags[0]['term'] for term in ("Cover Picture", "Cover Profile")]):
                print(f"IGNORING COVER: {tweet_body}\n")
                self.addToPosted(entry.id)
                return
        except Exception as e:
            pass

        media = None
        image = findImage(entry)
        image_file = downloadImage(image)
        if image_file:
            print(f"IMAGE: {image}")
            if self.api:
                media = [self.api.media_upload(image_file).media_id]
            os.remove(image_file)

        print(f"TWEET: {tweet_body}\n")
        if self.api:
            try:
                self.api.update_status(tweet_body, media_ids=media)
            except tweepy.error.TweepError as e:
                if e.api_code == 187:
                    print("ERROR: Tweet refused as duplicate\n")
                else:
                    print(f"ERROR: Tweet refused, {e.reason}\n")
                    sys.exit(1)

        self.addToPosted(entry.id)
        self.n_tweeted += 1

        if self.api:
            time.sleep(self.wait_time)

    # Main function, iterating over feeds and posting new items
    def run(self):
        for feed in self.feeds:
            parsed_feed = feedparser.parse(feed)
            for entry in parsed_feed.entries:
                if entryMatches(entry):
                    self.n_seen += 1
                    # If no ID provided, use the link as ID
                    if "id" not in entry:
                        entry.id = entry.link
                    if entry.id not in self.posted:
                        self.sendTweet(entry)
                        # Bail out if we have reached max number of tweets
                        if self.throttle > 0 and self.n_tweeted >= self.throttle:
                            print(f"Max number of papers met ({self.throttle}), stopping now")
                            return

    # Print statistics of a given run
    def printStats(self):
        print(f"Number of relevant papers: {self.n_seen}")
        print(f"Number of papers tweeted: {self.n_tweeted}")

    # Print out the n top tweets (most liked and RT'ed)
    def printTopTweets(self, count=20):
        tweets = self.api.user_timeline(count=200)
        oldest = tweets[-1].created_at
        print(f"Top {count} recent tweets, by number of RT and likes, since {oldest}:\n")

        tweets = [(t.retweet_count + t.favorite_count, t.id, t) for t in tweets]
        tweets.sort(reverse=True)
        for _, _, t in tweets[0:count]:
            url = f"https://twitter.com/{t.user.screen_name}/status/{t.id}"
            print(f"{t.retweet_count} RT {t.favorite_count} likes: {url}")
            print(f"    {t.created_at}")
            print(f"    {t.text}\n")


def main():
    # Make sure all options are correctly typed
    options_allowed = ["--do-not-tweet", "--top-tweets"]
    for arg in sys.argv[1:]:
        if arg not in options_allowed:
            print(f"Unknown option: {arg}")
            sys.exit(1)

    # Initialize our bot
    doTweet = "--do-not-tweet" not in sys.argv
    bot = PapersBot(doTweet)

    # We can print top tweets
    if "--top-tweets" in sys.argv:
        bot.printTopTweets()
        sys.exit(0)

    bot.run()
    bot.printStats()


if __name__ == "__main__":
    main()
