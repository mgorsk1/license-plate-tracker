from abc import abstractmethod
from json import dumps
from os import getenv, environ, remove
from time import time
from uuid import uuid4

import cv2
from google.cloud import storage
from inflection import underscore
from requests import post, HTTPError

from app.database import ResultDatabase, TemporaryDatabase
from app.config import log, BASE_PATH, config


class Executor:
    def __init__(self, reset_after):
        environ['GOOGLE_APPLICATION_CREDENTIALS'] = "{}/config/gcp/key.json".format(BASE_PATH)

        self.reset_after = reset_after

        self.index_name = underscore(self.__class__.__name__)
        self.rdb = ResultDatabase(config.get('RDB_HOST'),
                                  config.get('RDB_PORT'),
                                  self.index_name,
                                  **dict(db_user=config.get('RDB_USER'),
                                         db_pass=getenv('RDB_PASS'),
                                         db_scheme=config.get('RDB_SCHEME')))

        self.tdb = TemporaryDatabase(config.get('TDB_HOST'), config.get('TDB_PORT'), **dict(db_pass=getenv('TDB_PASS')))

        self.pushover_config = dict(APP_TOKEN=getenv("PUSHOVER_APP_TOKEN"), USER_KEY=getenv("PUSHOVER_USER_KEY"))

        client = Executor._get_storage_client()

        self.bucket = client.bucket(self.index_name)

        if not self.bucket.exists():
            self.bucket.create()
            self.bucket.make_public(recursive=True, future=True)

            log.info("#created public #gcp #bucket", extra=dict(bucket=self.index_name))

    @abstractmethod
    def action(self, value, confidence, image, **kwargs):
        raise NotImplementedError

    def run(self, value, confidence, image, **kwargs):
        run_uuid = uuid4()

        value = dumps(dict(time_added=time(), confidence=confidence)).encode('utf-8')

        if self.reset_after <= 0:
            self.tdb.set_key(value + ':Y', value)
        else:
            self.tdb.set_key(value + ':Y', value, ex=self.reset_after)

        self.take_action(value, confidence, image, run_uuid, **dict(kwargs))

    def take_action(self, value, confidence, image, uuid, **kwargs):
        if not self.rdb.check_if_exists('value', value, self.reset_after, False):
            log.info("#value does not exist in result #database", extra=dict(value=value))
            if not self.rdb.check_if_exists('value', value, self.reset_after, True):
                log.info("#similar #value does not exist in result #database", extra=dict(value=value))
                if not self.rdb.check_if_exists('candidates', value, self.reset_after, False):
                    log.info("#value does not exist amongst candidates in #database", extra=dict(value=value))

                    self.action(value, confidence, image, uuid, **dict(kwargs))

                    log.info("#notification send about value", extra=dict(value=value, confidence=confidence))
                else:
                    log.info("#value existed amongst candidates in #database", extra=dict(value=value))
            else:
                log.info("#value matched another #similar value in #database", extra=dict(value=value))
        else:
            log.info("#value existed in #database", extra=dict(value=value))

        pass

    def notify_pushover(self, title, message, url):
        """
        Send request to pushover_notify API with data:

        token (required) - your application"s API token
        user (required) - the user/group key (not e-mail address) of your user (or you), viewable when logged into our dashboard (often referred to as USER_KEY in our documentation and code examples)
        message (required) - your message
        Some optional parameters may be included:
        attachment - an image attachment to send with the message; see attachments for more information on how to upload files
        device - your user"s device name to send the message directly to that device, rather than all of the user"s devices (multiple devices may be separated by a comma)
        title - your message"s title, otherwise your app"s name is used
        url - a supplementary URL to show with your message
        url_title - a title for your supplementary URL, otherwise just the URL is shown
        priority - send as -2 to generate no notification/alert, -1 to always send as a quiet notification, 1 to display as high-priority and bypass the user"s quiet hours, or 2 to also require confirmation from the user
        sound - the name of one of the sounds supported by device clients to override the user"s default sound choice
        timestamp - a Unix timestamp of your message"s date and time to display to the user, rather than the time your message is received by our API

        :param title: title for message
        :param message: message
        :param url: optional url to pass
        :return: -
        """
        result = None
        device = "video-analysis"

        request_data = dict(token=self.pushover_config.get("APP_TOKEN", ""),
                            user=self.pushover_config.get("USER_KEY", ""),
                            message=message,
                            device=device,
                            title=title,
                            url=url)

        try:
            result = post("https://api.pushover.net/1/messages.json", data=request_data)

            log.info("#delivered #pushover #notification", extra=dict(pushover=dict(message=message,
                                                                                    title=title,
                                                                                    url=url,
                                                                                    device=device)))
        except HTTPError:
            log.error("error while #sending #pushover #notification", exc_info=True)

        return result

    def save_image_to_gcp(self, value, image, uuid):
        tmp_file = Executor.save_image_locally(value, image, uuid, 'tmp', 'png')

        filename = "{}_{}.png".format(value, uuid)

        blob = self.bucket.blob(filename)

        blob.upload_from_filename(tmp_file, content_type="image/png")

        remove(tmp_file)

        log.info("#image sent to #gcp #bucket", extra=dict(gcp=dict(bucket=self.index_name,
                                                                    fileName=filename,
                                                                    publicUrl=blob.public_url)))

        return blob.public_url

    @staticmethod
    def save_image_locally(value, image, uuid, folder, ext):
        filename = "{}_{}.{}".format(value, uuid, ext)

        full_path = "{}/{}/{}".format(BASE_PATH, folder, filename)

        cv2.imwrite(full_path, image)

        log.info("#saved #image", extra=dict(filePath=full_path, folder=folder, ext=ext))

        return full_path

    @staticmethod
    def _get_storage_client():
        return storage.Client(project=getenv('GCP_PROJECT_ID'))
