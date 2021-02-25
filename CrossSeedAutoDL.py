#!python3

import argparse
import json
import logging
import os
import re
import requests
import shutil
import time
from guessit import guessit
from urllib.parse import urlencode

parser = argparse.ArgumentParser(description='Searches for cross-seedable torrents')
parser.add_argument('-p', '--parse-dir', dest='parse_dir', action='store_true', help='Optional. Indicates whether to search for all the items inside the input directory as individual releases')
parser.add_argument('-d', '--delay', metavar='delay', dest='delay', type=int, default=10, help='Pause duration (in seconds) between searches (default: 10)')
parser.add_argument('-i', '--input-path', metavar='input_path', dest='input_path', type=str, required=True, help='File or Folder for which to find a matching torrent')
parser.add_argument('-s', '--save-path', metavar='save_path', dest='save_path', type=str, required=True, help='Directory in which to store downloaded torrents')
parser.add_argument('-u', '--url', metavar='jackett_url', dest='jackett_url', type=str, required=True, help='URL for your Jackett instance, including port number if needed')
parser.add_argument('-k', '--api-key', metavar='api_key', dest='api_key', type=str, required=True, help='API key for your Jackett instance')
parser.add_argument('-t', '--trackers', metavar='trackers', dest='trackers', type=str, default=None, required=False, help='Tracker(s) on which to search. Comma-separated if multiple (no spaces). If ommitted, all trackers will be searched.')
parser.add_argument('--ignore-history', dest='ignore_history', action='store_true', help='Optional. Indicates whether to ignore history file when conducting searches.')
parser.add_argument('--strict-size', dest='strict_size', action='store_true', help='Optional. Indicates whether to match torrent search result sizes to exactly the size of the input path. Might miss otherwise cross-seedtable torrents that contain additional files such as .nfo files')
ARGS = parser.parse_args()

ARGS.input_path = os.path.expanduser(ARGS.input_path)

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

formatter = logging.Formatter('\n%(asctime)s - Module: %(module)s - Line: %(lineno)d - Message: %(message)s')
file_handler = logging.FileHandler('CrossSeedAutoDL.log')
file_handler.setFormatter(formatter)

logger.addHandler(file_handler)

if os.name == 'nt':
    from ctypes import windll, wintypes
    FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
    GetFileAttributes = windll.kernel32.GetFileAttributesW


class ReleaseData:
    @staticmethod
    def get_release_data(path):
        return {
            'main_path': path, 
            'basename': os.path.basename(path), 
            'size': ReleaseData._get_total_size(path),
            'guessed_data': guessit( os.path.basename(path) )
        }

    @staticmethod
    def _get_total_size(path):
        if os.path.isfile(path):
            return ReleaseData._get_file_size(path)
        elif os.path.isdir(path):
            total_size = 0
            for root, dirs, filenames in os.walk(path):
                for filename in filenames:
                    filesize = ReleaseData._get_file_size(os.path.join(root, filename))
                    if filesize is None:
                        return None
                    total_size += filesize
            return total_size

    @staticmethod
    def _get_file_size(file_path):
        if ReleaseData._is_link(file_path):
            source_path = os.readlink(file_path)
            if os.path.isfile(source_path):
                return os.path.getsize(source_path)
            else:
                return None
        else:
            return os.path.getsize(file_path)

    @staticmethod
    def _is_link(file_path):
        if os.name == 'nt':
            if GetFileAttributes(file_path) & FILE_ATTRIBUTE_REPARSE_POINT:
                return True
            else:
                return False
        else:
            return os.path.islink(file_path)


class Searcher:
    # 1 MibiByte == 1024^2 bytes
    MiB = 1024**2
    # max size difference (in bytes) in order to account for extra or missing files, eg. nfo files
    size_differences_strictness = {True: 0, False: 5 * MiB}
    max_size_difference = size_differences_strictness[ARGS.strict_size]

    # keep these params in response json, discard the rest
    keys_from_result = ['Tracker', 'TrackerId', 'CategoryDesc', 'Title', 'Link', 'Details', 'Category', 'Size', 'Imdb']
    # torznab categories: 2000 for movies, 5000 for TV. This dict is for matching against the (str) types generated by 'guessit'
    category_types = {'movie': 2000, 'episode': 5000}

    def __init__(self):
        self.search_results = []

    def search(self, local_release_data, search_history):
        if self._is_skip_worthy(local_release_data, search_history):
            return []

        search_query = local_release_data['guessed_data']['title']
        if local_release_data['guessed_data'].get('year') is not None:
            search_query += ' ' + str( local_release_data['guessed_data']['year'] )

        search_url = self._get_full_search_url(search_query, local_release_data)
        logger.info(search_url)

        resp = None
        for n in range(2):
            try:
                resp = requests.get(search_url, local_release_data)
                break
            except requests.exceptions.ReadTimeout:
                if n == 0:
                    print(f'Connection timed out. Retrying once more.')
                    time.sleep(ARGS.delay)
            except requests.exceptions.ConnectionError:
                if n == 0:
                    print(f'Connection failed. Retrying once more.')
                    time.sleep(ARGS.delay)

        if not resp:
            return []
        ###
        # self._save_results(local_release_data); exit()
        try:
            resp_json = resp.json()
        except json.decoder.JSONDecodeError as e:
            print('Json decode error. Incident logged')
            logger.info(f'Json decode Error. Response text: {resp.text}')
            logger.exception(e)
            return []

        if resp_json['Indexers'] == []:
            info = 'No results found due to incorrectly input indexer names ({}). Check ' \
                   'your spelling/capitalization (are they added to Jackett?). This script has exited'.format(ARGS.trackers)
            print(info)
            logger.info(info)
            exit(1)

        # append basename to history
        if local_release_data['basename'] not in search_history['basenames_searched']:
            search_history['basenames_searched'].append(local_release_data['basename'])

        self.search_results = self._trim_results( resp_json['Results'] )
        return self._get_matching_results(local_release_data)

    # construct final search url
    @staticmethod
    def _get_full_search_url(search_query, local_release_data):
        base_url = ARGS.jackett_url.strip('/') + '/api/v2.0/indexers/all/results?'

        main_params = {
            'apikey': ARGS.api_key, 
            'Query': search_query
        }

        optional_params = {
            'Tracker[]': ARGS.trackers, 
            'Category[]': Searcher.category_types[ local_release_data['guessed_data']['type'] ], 
            'season': local_release_data['guessed_data'].get('season'),
            'episode': local_release_data['guessed_data'].get('episode')
        }

        for param, arg in optional_params.items():
            if arg is not None:
                main_params[param] = arg

        return base_url + urlencode(main_params)

    def _get_matching_results(self, local_release_data):
        matching_results = []
        # print(f'Parsing { len(self.search_results) } results. ', end='')

        for result in self.search_results:
            max_size_difference = self.max_size_difference
            # older torrents' sizes in blutopia are are slightly off
            if result['Tracker'] == 'Blutopia':
                max_size_difference *= 2

            if abs( result['Size'] - local_release_data['size'] ) <= max_size_difference:
                matching_results.append(result)

        print(f'{ len(matching_results) } matched of { len(self.search_results) } results.')
        logger.info(f'{ len(matching_results) } matched of { len(self.search_results) } results.')

        return matching_results

    # remove unnecessary values from results json
    def _trim_results(self, search_results):
        trimmed_results = []

        for result in search_results:
            new_result = {}
            for key in self.keys_from_result:
                new_result[key] = result[key]
            new_result['Title'] = self._reformat_release_name( new_result['Title'] )
            trimmed_results.append(new_result)
        return trimmed_results

    # some titles in jackett search results get extra data appended in square brackets,
    # ie. 'Movie.Name.720p.x264 [Golden Popcorn / 720p / x264]'
    @staticmethod
    def _reformat_release_name(release_name):
        release_name_re = r'^(.+?)( \[.*/.*\])?$'

        match = re.search(release_name_re, release_name, re.IGNORECASE)
        if match:
            return match.group(1)

        logger.info(f'"{release_name}" name could not be trimmed down')
        return release_name

    @staticmethod
    def _is_skip_worthy(local_release_data, search_history):
        # if --parse-dir omitted, search anyway. Download history will still be adhered to
        if not ARGS.ignore_history:
            if HistoryManager.is_file_previously_searched( local_release_data['basename'], search_history )\
                    and ARGS.parse_dir:
                print( 'Skipping search. File previously searched: {basename}'.format(**local_release_data) )
                logger.info( 'Skipping search. File previously searched: {basename}'.format(**local_release_data) )
                return True

        if local_release_data['size'] is None:
            print('Skipping. Could not get proper filesize data')
            logger.info('Skipping. Could not get proper filesize data')
            return True

        return False

    ###
    # def _save_results(self, local_release_data):
    #     search_results_path = os.path.join( os.path.dirname(os.path.abspath(__file__)), 'search_results.json' )
    #     target_dict = {'local_release_data': local_release_data, 'results': self.search_results}
    #
    #     with open(search_results_path, 'w', encoding='utf8') as f:
    #         json.dump([target_dict], f, indent=4)


class Downloader:
    # for the purpose of trimming a 'Description' URL down to its path only. Some trackers might have multiple proxies
    # ie. http://tracker.url1.net/details?9012 != http://tracker.url2.com/details?9012, but their path remain the same: /details?9012
    @staticmethod
    def download(result, search_history):
        release_name = Downloader._sanitize_name( '{Title} [{Tracker}]'.format( **result ) )

        # if torrent file is missing, ie. Blutopia
        if result['Link'] is None:
            print( f'- Skipping release (no download link): {release_name}' )
            logger.info( f'- Skipping release (no download link): {release_name}' )
            return
        if not ARGS.ignore_history:
            if HistoryManager.is_torrent_previously_grabbed(result, search_history):
                print( f'- Skipping download (previously grabbed): {release_name}' )
                logger.info( f'- Skipping download (previously grabbed): {release_name}' )
                return

        file_path = os.path.join( ARGS.save_path, release_name + '.torrent' )
        file_path = Downloader._validate_path(file_path)

        print(f'- Grabbing release: {release_name}')
        logger.info(f'- Grabbing release: {release_name}')

        response = requests.get(result['Link'], stream=True)
        with open(file_path, 'wb') as f:
            shutil.copyfileobj(response.raw, f)

        HistoryManager.append_to_download_history(result['Details'], result['TrackerId'], search_history)

    @staticmethod
    def _sanitize_name(release_name):
        release_name = release_name.replace('/', '-')
        release_name = re.sub(r'[^\w\-_.()\[\] ]+', '', release_name, flags=re.IGNORECASE)
        return release_name

    @staticmethod
    def _validate_path(file_path):
        filename, ext = os.path.splitext(file_path)

        n = 1
        while os.path.isfile(file_path):
            file_path = f'{filename} ({n}){ext}'
            n += 1

        return file_path


class HistoryManager:
    search_history_file_path = os.path.join( os.path.dirname(os.path.abspath(__file__)), 'SearchHistory.json' )
    # Some trackers may have several proxies. This ensures that only the url path is logged
    # eg. tracker1.proxy1.org/details?id=55 != tracker1.proxy9001.org/details?id=55, but '/details?id=55' remains the same
    url_path_re = r'^https?://[^/]+(.+)'

    @staticmethod
    def get_download_history():
        try:
            with open(HistoryManager.search_history_file_path, 'r', encoding='utf8') as f:
                search_history = json.load(f)
            return search_history
        except:
            return {
                'basenames_searched': [],
                'download_history': {}
            }

    @staticmethod
    def is_file_previously_searched(basename, search_history):
        for name in search_history['basenames_searched']:
            if basename == name:
                return True
        return False

    @staticmethod
    def is_torrent_previously_grabbed(result, search_history):
        url_path = re.search( HistoryManager.url_path_re, result['Details'] ).group(1)
        tracker_id = result['TrackerId']

        if search_history['download_history'].get(tracker_id) is None:
            return False

        for download_history_url_path in search_history['download_history'][tracker_id]:
            if download_history_url_path == url_path:
                return True
        return False

    @staticmethod
    def append_to_download_history(details_url, tracker_id, search_history):
        url_path = re.search(HistoryManager.url_path_re, details_url).group(1)

        if search_history['download_history'].get(tracker_id) is None:
            search_history['download_history'][tracker_id] = []

        # to prevent duplicates, in case --ignore-history flag is enabled
        if url_path not in search_history['download_history'][tracker_id]:
            search_history['download_history'][tracker_id].append(url_path)


def main():
    assert_settings()
    paths = [ os.path.normpath(ARGS.input_path)] if not ARGS.parse_dir \
        else [os.path.join(ARGS.input_path, f) for f in os.listdir(ARGS.input_path) ]

    search_history = HistoryManager.get_download_history()

    for i, path in enumerate(paths):
        local_release_data = ReleaseData.get_release_data(path)

        if local_release_data['guessed_data'].get('title') is None:
            print( 'Skipping file. Could not get title from filename: {}'.format(local_release_data['basename']) )
            logger.info( 'Skipping file. Could not get title from filename: {}'.format(local_release_data['basename']) )
            continue

        info = 'Searching for {num} of {size}: {title} {year}'.format(
            num=i + 1,
            size=len(paths),
            title=local_release_data['guessed_data']['title'], 
            year=local_release_data['guessed_data'].get('year', '')
            )
        print(info)
        logger.info(info + f'/ {os.path.basename(path)}')

        searcher = Searcher()
        matching_results = searcher.search(local_release_data, search_history)
        ###
        # [print(f['Title']) for f in matching_results]
        for result in matching_results:
            Downloader.download(result, search_history)

        time.sleep(ARGS.delay)

    # write back to download history file
    with open(HistoryManager.search_history_file_path, 'w', encoding='utf8') as f:
        json.dump(search_history, f, indent=4)
    

def assert_settings():
    assert os.path.exists(ARGS.input_path), f'"{ARGS.input_path}" does not exist'
    if ARGS.parse_dir:
        assert os.path.isdir(ARGS.input_path), f'"{ARGS.input_path}" is not a directory. The -p/--parse-dir flag will parse the contents within the input path as individual releases'
    assert os.path.isdir(ARGS.save_path), f'"{ARGS.save_path}" directory does not exist'

    assert ARGS.jackett_url.startswith('http'), 'Error: Jackett URL must start with http / https'

    try:
        resp = requests.head(ARGS.jackett_url)
    except requests.exceptions.RequestException as e:
        print(f'"{ARGS.jackett_url}" cannot be reached: {e}')
        exit()


if __name__ == '__main__':
    main()
