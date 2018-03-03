#!/usr/bin/env python

import os.path
import threading

import time
import logging
import argparse

import re
import requests
import xml.etree.ElementTree
import copy

from requests.exceptions import ConnectionError
from termcolor import colored

logging.VERBOSE = (logging.INFO + logging.DEBUG) // 2

logger = logging.getLogger('dash-proxy')

ns = {'mpd':'urn:mpeg:dash:schema:mpd:2011'}


class Formatter(logging.Formatter):
    def __init__(self, fmt=None, datefmt=None):
        super(Formatter, self).__init__(fmt, datefmt)

    def format(self, record):
        color = None
        if record.levelno == logging.ERROR:
            color = 'red'
        if record.levelno == logging.INFO:
            color = 'green'
        if record.levelno == logging.WARNING:
            color = 'yellow'
        if color:
            return colored(record.msg, color)
        else:
            return record.msg


ch = logging.StreamHandler()
ch.setLevel(logging.DEBUG)
formatter = Formatter()
ch.setFormatter(formatter)
logger.addHandler(ch)

def baseUrl(url):
    idx = url.rfind('/')
    if idx >= 0:
        return url[:idx+1]
    else:
        return url

class RepAddr(object):
    def __init__(self, period_idx, adaptation_set_idx, representation_idx):
        self.period_idx = period_idx
        self.adaptation_set_idx = adaptation_set_idx
        self.representation_idx = representation_idx

    def __str__(self):
        return 'Representation (period=%d adaptation-set=%d representation=%d)' % (self.period_idx, self.adaptation_set_idx, self.representation_idx)


class MpdLocator(object):
    def __init__(self, mpd):
        self.mpd = mpd

    def representation(self, rep_addr):
        return self.adaptation_set(rep_addr).findall('mpd:Representation', ns)[rep_addr.representation_idx]

    def segment_template(self, rep_addr):
        rep_st = self.representation(rep_addr).find('mpd:SegmentTemplate', ns)
        if rep_st is not None:
            return rep_st
        else:
            return self.adaptation_set(rep_addr).find('mpd:SegmentTemplate', ns)

    def segment_timeline(self, rep_addr):
        return self.segment_template(rep_addr).find('mpd:SegmentTimeline', ns)

    def adaptation_set(self, rep_addr):
        return self.mpd.findall('mpd:Period', ns)[rep_addr.period_idx].findall('mpd:AdaptationSet', ns)[rep_addr.adaptation_set_idx]


class HasLogger(object):
    def verbose(self, msg):
        self.logger.log(logging.VERBOSE, msg)

    def info(self, msg):
        self.logger.log(logging.INFO, msg)

    def debug(self, msg):
        self.logger.log(logging.DEBUG, msg)

    def warning(self, msg):
        self.logger.log(logging.WARNING, msg)

    def error(self, msg):
        self.logger.log(logging.ERROR, msg)

class DashProxy(HasLogger):
    retry_interval = 10

    def __init__(self, mpd, output_dir, download, save_mpds=False):
        self.logger = logger

        self.mpd = mpd
        self.output_dir = output_dir
        if not os.path.isdir(output_dir):
            os.makedirs(output_dir)
        self.download = download
        self.save_mpds = save_mpds
        self.i_refresh = 0

        self.downloaders = {}
        self.stop = False

    def run(self):
        logger.log(logging.INFO, 'Running dash proxy for stream %s. Output goes in %s' % (self.mpd, self.output_dir))
        i = 0
        while not self.stop:
            logger.info('get mpd')
            success = False
            max_tries = 5
            current_try = 0
            while not success and current_try < max_tries:
                try:
                    r = requests.get(self.mpd)
                    if r.status_code < 200 or r.status_code >= 300:
                        logger.error('Cannot GET the MPD. Server returned %s. Retry %d' % (r.status_code, current_try))
                        current_try = current_try + 1
                except ConnectionError:
                    logger.error('Connection error. Retry %d' % current_try)
                    current_try = current_try + 1
                else:
                    success = True
                    xml.etree.ElementTree.register_namespace('', ns['mpd'])
                    mpd = xml.etree.ElementTree.fromstring(r.text)
                    thread = threading.Thread(target=self.handle_mpd, args=(mpd,))
                    thread.start()
                    logger.info('thread %d created' % i)
                    i = i+1

                    minimum_update_period = mpd.attrib.get('minimumUpdatePeriod', '0')
                    if minimum_update_period:
                        minimum_update_period = int(re.search('\d+',minimum_update_period).group(0)) + 1
                        time.sleep(minimum_update_period)
                    else:
                        self.info('VOD MPD. Nothing more to do. Stopping...')
                        self.stop = True
            if not success:
                self.stop = True

    def get_base_url(self, mpd):
        base_url = baseUrl(self.mpd)
        location = mpd.find('mpd:Location', ns)
        if location is not None:
            base_url = baseUrl(location.text)
        baseUrlNode = mpd.find('mpd:BaseUrl', ns)
        if baseUrlNode:
            if baseUrlNode.text.startswith('http://') or baseUrlNode.text.startswith('https://'):
                base_url = baseUrl(baseUrlNode.text)
            else:
                base_url = base_url + baseUrlNode.text
        return base_url

    def handle_mpd(self, mpd):
        original_mpd = copy.deepcopy(mpd)

        periods = mpd.findall('mpd:Period', ns)
        logger.debug('mpd=%s' % (periods,))
        logger.debug('Found %d periods choosing the 1st one' % (len(periods),))
        period = periods[0]
        for as_idx, adaptation_set in enumerate( period.findall('mpd:AdaptationSet', ns) ):
            max_bandwidth = 0
            max_rep_idx = 0
            for rep_idx, representation in enumerate( adaptation_set.findall('mpd:Representation', ns) ):
                cur_bandwidth = int(representation.attrib.get('bandwidth'))
                if cur_bandwidth > max_bandwidth:
                    max_bandwidth = cur_bandwidth
                    max_rep_idx = rep_idx
            self.verbose('Found representation with id %s' % max_rep_idx)
            rep_addr = RepAddr(0, as_idx, max_rep_idx)
            self.ensure_downloader(mpd, rep_addr)

        #self.write_output_mpd(original_mpd)

    def ensure_downloader(self, mpd, rep_addr):
        if rep_addr in self.downloaders:
            self.verbose('A downloader for %s already started' % (rep_addr,))
        else:
            self.info('Starting a downloader for %s' % (rep_addr,))
            downloader = DashDownloader(self, rep_addr)
            self.downloaders[rep_addr] = downloader
            if downloader.handle_mpd(mpd, self.get_base_url(mpd)):
                self.stop = True

    def write_output_mpd(self, mpd):
        self.info('Writing the update MPD file')
        content = xml.etree.ElementTree.tostring(mpd, encoding="utf-8").decode("utf-8")
        dest = os.path.join(self.output_dir, 'manifest.mpd')

        with open(dest, 'wt') as f:
            f.write(content)

        if self.save_mpds:
            dest = os.path.join(self.output_dir, 'manifest.{}.mpd'.format(self.i_refresh))
            with open(dest, 'wt') as f:
                f.write(content)


class DashDownloader(HasLogger):
    def __init__(self, proxy, rep_addr):
        self.logger = logger
        self.proxy = proxy
        self.rep_addr = rep_addr
        self.mpd_base_url = ''

        self.initialization_downloaded = False

    def handle_mpd(self, mpd, base_url):
        self.mpd_base_url = base_url
        self.mpd = MpdLocator(mpd)

        rep = self.mpd.representation(self.rep_addr)
        segment_template = self.mpd.segment_template(self.rep_addr)
        segment_timeline = self.mpd.segment_timeline(self.rep_addr)

        initialization_template = segment_template.attrib.get('initialization', '')
        if initialization_template and not self.initialization_downloaded:
            self.initialization_downloaded = True
            self.download_template(initialization_template, rep)

        segments = copy.deepcopy(segment_timeline.findall('mpd:S', ns))
        idx = 0
        for segment in segments:
            duration = int( segment.attrib.get('d', '0') )
            repeat = int( segment.attrib.get('r', '0') )
            idx = idx + 1
            for _ in range(0, repeat):
                elem = xml.etree.ElementTree.Element('{urn:mpeg:dash:schema:mpd:2011}S', attrib={'d':duration})
                segment_timeline.insert(idx, elem)
                self.verbose('appding a new elem')
                idx = idx + 1

        media_template = segment_template.attrib.get('media', '')
        nex_time = 0
        for segment in segment_timeline.findall('mpd:S', ns):
            current_time = int(segment.attrib.get('t', '-1'))
            if current_time == -1:
                segment.attrib['t'] = next_time
            else:
                next_time = current_time
            next_time += int(segment.attrib.get('d', '0'))
            if self.download_template(media_template, rep, segment):
                return True

    def download_template(self, template, representation=None, segment=None):
        dest_filename = self.render_template(template, representation, segment)
        local_filename = dest_filename[dest_filename.rfind('/')+1:]
        if os.path.isfile(os.path.join(self.proxy.output_dir, local_filename)):
            self.debug('File %s already exists' % local_filename)
        else:
            dest_url = self.full_url(dest_filename)
            self.debug('requesting %s from %s' % (local_filename, dest_url))
            r = requests.get(dest_url)
            if r.status_code >= 200 and r.status_code < 300:
                self.write(local_filename, r.content)
            else:
                self.error('cannot download %s server returned %d' % (dest_url, r.status_code))
                if r.status_code == 410:
                    return True

    def render_template(self, template, representation=None, segment=None):
        template = template.replace('$RepresentationID$', '{representation_id}')
        template = template.replace('$Time$', '{time}')

        args = {}
        if representation is not None:
            args['representation_id'] = representation.attrib.get('id', '')
        if segment is not None:
            args['time'] = segment.attrib.get('t', '')

        template = template.format(**args)
        return template

    def full_url(self, dest):
        return self.mpd_base_url + dest # TODO remove hardcoded arrd

    def write(self, dest, content):
        dest = dest.split('?')[0]
        dest = os.path.join(self.proxy.output_dir, dest)
        f = open(dest, 'wb')
        f.write(content)
        f.close()


def run(args):
    logger.setLevel(logging.VERBOSE if args.v else logging.INFO)
    proxy = DashProxy(mpd=args.mpd,
                  output_dir=args.o,
                  download=args.d,
                  save_mpds=args.save_individual_mpds)
    return proxy.run()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mpd")
    parser.add_argument("-v", action="store_true")
    parser.add_argument("-d", action="store_true")
    parser.add_argument("-o", default='.')
    parser.add_argument("--save-individual-mpds", action="store_true")
    args = parser.parse_args()

    run(args)

if __name__ == '__main__':
    main()
