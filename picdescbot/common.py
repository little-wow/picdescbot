# coding=utf-8
# picdescbot: a tiny twitter/tumblr bot that tweets random pictures from wikipedia and their descriptions
# this file contains common basic functionality of the bot, such as getting the picture and description
# Copyright (C) 2016 Elad Alfassa <elad@fedoraproject.org>

from __future__ import unicode_literals, absolute_import, print_function

from wordfilter import Wordfilter
import json
import re
import requests
import time
from io import BytesIO

MEDIAWIKI_API = "https://commons.wikimedia.org/w/api.php"
CVAPI = "https://api.projectoxford.ai/vision/v1.0/analyze"

HEADERS = {"User-Agent":  "picdescbot, http://github.com/elad661/picdescbot"}

supported_formats = re.compile('\.(png|jpe?g|gif)$', re.I)
word_filter = Wordfilter()
word_filter.add_words(['nazi'])  # I really don't want the bot to show this kind of imagery!
category_blacklist = ['september 11']  # Blacklist some categories, just in case

# Gender neutralization helps prevent accidental transphobic juxtapositions
# which can occur when CVAPI uses gendered words in the description, but their
# gender detection is wrong. Computers shouldn't try to detect gender, and
# always be neautral. You can't know someone's gender just by how they look!
gendered_words = {'woman': 'person',
                  'man': 'person',
                  'women': 'people',
                  'men': 'people'}


def gender_neutralize(phrase):
    "Replace gendered words in the phrase with neutral ones"
    neutralized = []
    for word in phrase.lower().split():
        if word in gendered_words:
            word = gendered_words[word]
        neutralized.append(word)
    neutralized = ' '.join(neutralized)
    if neutralized != phrase:
        print('Gender neutralized: "{0}" => "{1}"'.format(phrase, neutralized))
    return neutralized


def get_random_picture():
    """Get a random picture from Wikimedia Commons.
    Returns None when the result is bad"""

    params = {"action": "query",
              "generator": "random",
              "grnnamespace": "6",
              "prop": "imageinfo|categories",
              "iiprop": "url|size|extmetadata|mediatype",
              "iiurlheight": "1080",
              "format": "json"}
    response = requests.get(MEDIAWIKI_API,
                            params=params,
                            headers=HEADERS).json()
    page = list(response['query']['pages'].values())[0]  # This API is ugly
    imageinfo = page['imageinfo'][0]
    extra_metadata = imageinfo['extmetadata']

    # We got a picture, now let's verify we can use it.
    if word_filter.blacklisted(page['title']):  # Check file name for bad words
        print('badword ' + page['title'])
        return None
    # Check picture title for bad words
    if word_filter.blacklisted(extra_metadata['ObjectName']['value']):
        print('badword ' + extra_metadata['ObjectName']['value'])
        return None
    # Check restrictions for more bad words
    if word_filter.blacklisted(extra_metadata['Restrictions']['value']):
        print('badword ' + extra_metadata['ObjectName']['value'])
        return None

    for category in page['categories']:
        for blacklisted_category in category_blacklist:
            if blacklisted_category in category['title'].lower():
                print('discarded, category blacklist: ' + category['name'])
                return None

    # Now check that the file is useable
    if imageinfo['mediatype'] != "BITMAP":
        return None

    # Make sure the image is big enough
    if imageinfo['width'] <= 50 or imageinfo['height'] <= 50:
        return None

    if not supported_formats.search(imageinfo['url']):
        return None
    else:
        return imageinfo


class CVAPIClient(object):
    "Microsoft Cognitive Services Client"
    def __init__(self, apikey):
        self.apikey = apikey

    def describe_picture(self, url):
        "Get description for a picture using Microsoft Cognitive Services"
        params = {'visualFeatures': 'Description,Adult'}
        json = {'url': url}
        headers = {'Content-Type': 'application/json',
                   'Ocp-Apim-Subscription-Key': self.apikey}

        result = None
        retries = 0

        while retries < 15 and not result:
            response = requests.post(CVAPI, json=json, params=params,
                                     headers=headers)
            if response.status_code == 429:
                print ("Message: %s" % (response.json()))
                if retries < 15:
                    time.sleep(2)
                    retries += 1
                else:
                    print('Error: failed after retrying!')

            elif response.status_code == 200 or response.status_code == 201:

                if 'content-length' in response.headers and int(response.headers['content-length']) == 0:
                    result = None
                elif 'content-type' in response.headers and isinstance(response.headers['content-type'], str):
                    if 'application/json' in response.headers['content-type'].lower():
                        result = response.json() if response.content else None
                    elif 'image' in response.headers['content-type'].lower():
                        result = response.content
            else:
                print("Error code: %d" % (response.status_code))
                print("url: %s" % url)
                print(response.json())
                retries += 1
                time.sleep(10 + retries*3)

        return result

    def get_picture_and_description(self, max_retries=20):
        "Get a picture and a description. Retries until a usable result is produced or max_retries is reached."
        pic = None
        retries = 0
        while retries <= max_retries:  # retry max 20 times, until we get something good
            while pic is None:
                pic = get_random_picture()
                if pic is None:
                    # We got a bad picture, let's wait a bit to be polite to the API server
                    time.sleep(1)
            url = pic['url']
            # Use a scaled-down image if the original is too big
            if pic['size'] > 4000000:
                url = pic['thumburl']

            result = self.describe_picture(url)

            if result is not None:
                description = result['description']
                if not result['adult']['isAdultContent']:  # no nudity and such
                    if len(description['captions']) > 0:
                        caption = description['captions'][0]['text']
                        caption = gender_neutralize(caption)
                        if not word_filter.blacklisted(caption):
                            return Result(caption, description['tags'], url,
                                          pic['descriptionshorturl'])
                        else:
                            print("caption discarded due to blacklist: " +
                                  caption)
                    else:
                        print("No caption for url: {0}".format(url))
                else:
                    print("Adult content. Discarded.")
                    print(url)
                    print(description)
            retries += 1
            print("Not good, retrying...")
            pic = None
            time.sleep(3)  # sleep to be polite to the API servers

        raise Exception("Maximum retries exceeded, no good picture")


class Result(object):
    "Represents a picture and its description"
    def __init__(self, caption, tags, url, source_url):
        self.caption = caption
        self.tags = tags
        self.url = url
        self.source_url = source_url

    def download_picture(self):
        "Returns a BytesIO object for an image URL"
        retries = 0
        picture = None
        print("downloading " + self.url)
        while retries <= 20:
            if retries > 0:
                print('Trying again...')
            response = requests.get(self.url, headers=HEADERS)
            if response.status_code == 200:
                picture = BytesIO(response.content)
                return picture
            else:
                print("Fetching picture failed: " + response.status_code)
                retries += 1
                time.sleep(1)
        raise Exception("Maximum retries exceeded when downloading a picture")