#!/usr/bin/python
"""
appleupdates.py

Utilities for dealing with Apple Software Update.

"""
# Copyright 2009-2011 Greg Neagle.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


import gzip
import hashlib
import os
import stat
import subprocess
import time
import urllib2
import urlparse
from xml.dom import minidom

from Foundation import NSDate
from Foundation import CFPreferencesCopyAppValue
# from Foundation import CFPreferencesCopyValue
from Foundation import CFPreferencesSetValue
from Foundation import CFPreferencesAppSynchronize
# from Foundation import kCFPreferencesAnyUser
from Foundation import kCFPreferencesCurrentUser
from Foundation import kCFPreferencesCurrentHost
from LaunchServices import LSFindApplicationForInfo

import FoundationPlist
import fetch
import launchd
import munkicommon
import munkistatus


# Apple Software Update Catalog URLs.
DEFAULT_CATALOG_URLS = {
    '10.5': 'http://swscan.apple.com/content/catalogs/others/index-leopard.merged-1.sucatalog',
    '10.6': 'http://swscan.apple.com/content/catalogs/others/index-leopard-snowleopard.merged-1.sucatalog',
    '10.7': 'http://swscan.apple.com/content/catalogs/others/index-lion-snowleopard-leopard.merged-1.sucatalog.gz',
}

# Preference domain for Apple Software Update.
APPLE_SOFTWARE_UPDATE_PREFS_DOMAIN = 'com.apple.SoftwareUpdate'

# Filename for results of softwareupdate -l -f <pathname>.
# This lists the currently applicable Apple updates in a
# very useful format.
APPLICABLE_UPDATES = 'ApplicableUpdates.plist'

# Path to the directory where local catalogs are stored, relative to
# munkicommon.pref('ManagedInstallDir') + /swupd/mirror/.
LOCAL_CATALOG_DIR_REL_PATH = 'content/catalogs/'

# The pristine, untouched, but potentially gzipped catalog.
APPLE_DOWNLOAD_CATALOG_NAME = 'apple.sucatalog'

# The pristine, untouched, and extracted catalog.
APPLE_EXTRACTED_CATALOG_NAME = 'apple_index.sucatalog'

# The catalog containing only updates in APPLICABLE_UPDATES.
# This is used to replicate a subset of the software update
# server data to our local cache.
FILTERED_CATALOG_NAME = 'filtered_index.sucatalog'

# The catalog containing only updates to be downloaded and installed.
# We use this one when downloading Apple updates.
# In this case package URLs are still pointing to the
# software update server so we can download them, but the rest of the
# URLs point to our local cache.
LOCAL_DOWNLOAD_CATALOG_NAME = 'local_download.sucatalog'

# Catalog with all URLs (including package URLs) pointed to local cache.
# We use this one during install phase.
# This causes softwareupdate -d -a to fail cleanly if we don't
# have the required packages already downloaded.
LOCAL_CATALOG_NAME = 'local_install.sucatalog'


class Error(Exception):
    """Class for domain specific exceptions."""


# TODO(unassigned): Break this out into different exceptions; it's used widely.
class ReplicationError(Error):
    """A custom error when replication fails."""


class CatalogNotFoundError(Error):
    """A catalog was not found."""


class AppleUpdates(object):

    """Class to installation of Apple Software Updates within Munki.

    This class handles update detection, as well as downloading and installation
    of those updates.
    """

    RESTART_ACTIONS = ['RequireRestart', 'RecommendRestart']

    def __init__(self):
        self._managed_install_dir = munkicommon.pref('ManagedInstallDir')

        self.cache_dir = os.path.join(self._managed_install_dir, 'swupd')
        self.temp_cache_dir = os.path.join(self.cache_dir, 'mirror')
        self.local_catalog_dir = os.path.join(
            self.cache_dir, LOCAL_CATALOG_DIR_REL_PATH)

        self.apple_updates_plist = os.path.join(
            self._managed_install_dir, 'AppleUpdates.plist')
        self.applicable_updates_plist = os.path.join(
            self.cache_dir, APPLICABLE_UPDATES)

        self.apple_download_catalog_path = os.path.join(
            self.temp_cache_dir, APPLE_DOWNLOAD_CATALOG_NAME)

        self.filtered_catalog_path = os.path.join(
            self.local_catalog_dir, FILTERED_CATALOG_NAME)
        self.local_catalog_path = os.path.join(
            self.local_catalog_dir, LOCAL_CATALOG_NAME)
        self.extracted_catalog_path = os.path.join(
            self.local_catalog_dir, APPLE_EXTRACTED_CATALOG_NAME)
        self.filtered_catalog_path = os.path.join(
            self.local_catalog_dir, FILTERED_CATALOG_NAME)
        self.local_download_catalog_path = os.path.join(
            self.local_catalog_dir, LOCAL_DOWNLOAD_CATALOG_NAME)

        self._update_list_cache = None

    def _ResetMunkiStatusAndDisplayMessage(self, message):
        """Resets MunkiStatus detail/percent, logs and msgs GUI.

        Args:
          message: str message to display to the user and log.
        """
        munkicommon.display_status_major(message)

    def _GetURLPath(self, full_url):
        """Returns only the URL path.

        Args:
          full_url: a str URL, complete with schema, domain, path, etc.
        Returns:
          The str path of the URL.
        """
        return urlparse.urlsplit(full_url)[2]  # (schema, netloc, path, ...)

    def RewriteURL(self, full_url):
        """Rewrites a single URL to point to our local replica.

        Args:
          full_url: a str URL, complete with schema, domain, path, etc.
        Returns:
          A str URL, rewritten if needed to point to the local cache.
        """
        local_base_url = 'file://localhost' + urllib2.quote(self.cache_dir)
        if full_url.startswith(local_base_url):
            return full_url  # url is already local, so just return it.
        return local_base_url + self._GetURLPath(full_url)

    def RewriteProductURLs(self, product, rewrite_pkg_urls=False):
        """Rewrites URLs in the product to point to our local cache.

        Args:
          product: list, of dicts, product info. This dict is changed by
              this function.
          rewrite_pkg_urls: bool, default False, if True package URLs are
              rewritten, otherwise only MetadataURLs are rewritten.
        """
        if 'ServerMetadataURL' in product:
            product['ServerMetadataURL'] = self.RewriteURL(
                product['ServerMetadataURL'])
        for package in product.get('Packages', []):
            if rewrite_pkg_urls and 'URL' in package:
                package['URL'] = self.RewriteURL(package['URL'])
            if 'MetadataURL' in package:
                package['MetadataURL'] = self.RewriteURL(
                    package['MetadataURL'])
        distributions = product['Distributions']
        for dist_lang in distributions.keys():
            distributions[dist_lang] = self.RewriteURL(
                distributions[dist_lang])

    def RewriteCatalogURLs(self, catalog, rewrite_pkg_urls=False):
        """Rewrites URLs in a catalog to point to our local replica.

        Args:
          rewrite_pkg_urls: Boolean, if True package URLs are rewritten,
              otherwise only MetadataURLs are rewritten.
        """
        if not 'Products' in catalog:
            return

        for product_key in catalog['Products'].keys():
            p = catalog['Products'][product_key]
            self.RewriteProductURLs(p, rewrite_pkg_urls=rewrite_pkg_urls)

    def RetrieveURLToCacheDir(self, full_url, copy_only_if_missing=False):
        """Downloads a URL and stores it in the same relative path on our
        filesystem. Returns a path to the replicated file.

        Args:
          full_url: str, full URL to retrieve.
          copy_only_if_missing: boolean, True to copy only if the file is not
              already cached, False to copy regardless of existence in cache.
        Returns:
          String path to the locally cached file.
        """
        relative_url = os.path.normpath(self._GetURLPath(full_url).lstrip('/'))
        local_file_path = os.path.join(self.cache_dir, relative_url)
        local_dir_path = os.path.dirname(local_file_path)
        if copy_only_if_missing and os.path.exists(local_file_path):
            return local_file_path
        if not os.path.exists(local_dir_path):
            try:
                os.makedirs(local_dir_path)
            except OSError, oserr:
                raise ReplicationError(oserr)
        try:
            self.GetSoftwareUpdateResource(
                full_url, local_file_path, resume=True)
        except fetch.MunkiDownloadError, err:
            raise ReplicationError(err)
        return local_file_path

    def GetSoftwareUpdateResource(self, url, destinationpath, resume=False):
        """Gets item from Apple Software Update Server.

        Args:
          url: str, URL of the resource to download.
          destinationpath: str, path of the destination to save the resource.
          resume: boolean, True to resume downloads, False to redownload.
        Returns:
          Boolean. True if a new download was required, False if the item was
          already in the local cache.
        """
        machine = munkicommon.getMachineFacts()
        darwin_version = os.uname()[2]
        # Set the User-Agent header to match that used by Apple's
        # softwareupdate client for better compatibility.
        user_agent_header = (
            "User-Agent: managedsoftwareupdate/%s Darwin/%s (%s) (%s)"
            % (machine['munki_version'], darwin_version,
               machine['arch'], machine['machine_model']))
        return fetch.getResourceIfChangedAtomically(
            url,
            destinationpath,
            custom_headers=[user_agent_header],
            resume=resume,
            follow_redirects=True)

    def CacheUpdateMetadata(self):
        """Copies ServerMetadata (.smd), Metadata (.pkm), and
        Distribution (.dist) files for the available updates to the local
        machine and writes a new sucatalog that refers to the local copies
        of these files."""
        catalog = FoundationPlist.readPlist(self.filtered_catalog_path)
        if not 'Products' in catalog:
            return

        for product_key in catalog['Products'].keys():
            if munkicommon.stopRequested():
                break
            munkicommon.display_status_minor(
                'Caching metadata for product ID %s', product_key)
            product = catalog['Products'][product_key]
            if 'ServerMetadataURL' in product:
                self.RetrieveURLToCacheDir(
                    product['ServerMetadataURL'], copy_only_if_missing=True)

            for package in product.get('Packages', []):
                if munkicommon.stopRequested():
                    break
                if 'MetadataURL' in package:
                    munkicommon.display_status_minor(
                        'Caching package metadata for product ID %s',
                        product_key)
                    self.RetrieveURLToCacheDir(
                        package['MetadataURL'], copy_only_if_missing=True)

            distributions = product['Distributions']
            for dist_lang in distributions.keys():
                if munkicommon.stopRequested():
                    break
                munkicommon.display_status_minor(
                    'Caching %s distribution for product ID %s',
                    dist_lang, product_key)
                dist_url = distributions[dist_lang]
                self.RetrieveURLToCacheDir(
                    dist_url, copy_only_if_missing=True)

        if munkicommon.stopRequested():
            return

        if not os.path.exists(self.local_catalog_dir):
            try:
                os.makedirs(self.local_catalog_dir)
            except OSError, oserr:
                raise ReplicationError(oserr)

        # rewrite metadata URLs to point to local caches.
        self.RewriteCatalogURLs(catalog, rewrite_pkg_urls=False)
        FoundationPlist.writePlist(
            catalog, self.local_download_catalog_path)

        # rewrite all URLs, including pkgs, to point to local caches.
        self.RewriteCatalogURLs(catalog, rewrite_pkg_urls=True)
        FoundationPlist.writePlist(catalog, self.local_catalog_path)

    def _GetPreferredLocalization(self, list_of_localizations):
        '''Picks the best localization from a list of available
        localizations.'''
        try:
            from Foundation import NSBundle
        except ImportError:
            # Foundation NSBundle isn't available
            languages = ['English', 'en']
            for language in languages:
                if language in list_of_localizations:
                    return language
        else:
            preferred_langs = \
                NSBundle.preferredLocalizationsFromArray_forPreferences_(
                    list_of_localizations, None)
            if preferred_langs:
                return preferred_langs[0]

        # first fallback, return en or English
        if 'English' in list_of_localizations:
            return 'English'
        elif 'en' in list_of_localizations:
            return 'en'

        # if we get this far, just return the first language
        # in the list of available languages
        return list_of_localizations[0]

    def GetDistributionForProductKey(self, product_key):
        '''Returns the path to a distibution file from the local cache for the given
        product_key.'''
        try:
            catalog = FoundationPlist.readPlist(self.local_catalog_path)
        except FoundationPlist.NSPropertyListSerializationException:
            return None
        product = catalog.get('Products', {}).get(product_key, {})
        if product:
            distributions = product.get('Distributions', {})
            if distributions:
                available_languages = distributions.keys()
                preferred_language = self._GetPreferredLocalization(
                    available_languages)
                fileurl = distributions[preferred_language]
                if fileurl.startswith('file://localhost'):
                    fileurl = fileurl[len('file://localhost'):]
                    return urllib2.unquote(fileurl)
        return None

    def GetBlockingApps(self, product_key):
        '''Given a product key, finds the cached softwareupdate dist file, 
        then parses it, looking for must-close apps and converting them to
        Munki's blocking_applications'''

        distfile = self.GetDistributionForProductKey(product_key)
        if not distfile:
            return []

        dom = minidom.parse(distfile)

        must_close_app_ids = []
        must_close_items = dom.getElementsByTagName('must-close')
        for item in must_close_items:
            apps = item.getElementsByTagName('app')
            for app in apps:
                keys = app.attributes.keys()
                if 'id' in keys:
                    must_close_app_ids.append(app.attributes['id'].value)

        blocking_apps = []
        for id in must_close_app_ids:
            resultcode, fileref, nsurl = LSFindApplicationForInfo(
                0, id, None, None, None)
            fileurl = str(nsurl)
            if fileurl.startswith('file://localhost'):
                fileurl = fileurl[len('file://localhost'):]
                pathname = urllib2.unquote(fileurl).rstrip('/')
                appname = os.path.basename(pathname)
                blocking_apps.append(appname)

        return blocking_apps

    def _WriteFilteredCatalog(self, product_ids, catalog_path):
        """Write out a sucatalog containing only the updates in product_ids.

        Args:
          product_ids: list of str, ProductIDs.
          catalog_path: str, path of catalog to write.
        """
        catalog = FoundationPlist.readPlist(self.extracted_catalog_path)
        product_ids = set(product_ids)  # convert to set for O(1) lookups.
        for product_id in list(catalog.get('Products', [])):
            if product_id not in product_ids:
                del catalog['Products'][product_id]
        FoundationPlist.writePlist(catalog, catalog_path)

    def IsRestartNeeded(self):
        """Returns True if any update requires an restart."""
        try:
            apple_updates = FoundationPlist.readPlist(self.apple_updates_plist)
        except FoundationPlist.NSPropertyListSerializationException:
            return True
        for item in apple_updates.get('AppleUpdates', []):
            if item.get('RestartAction') in self.RESTART_ACTIONS:
                return True
        # if we get this far, there must be no items that require restart
        return False

    def ClearAppleUpdateInfo(self):
        """Clears Apple update info.

        This is called after performing munki updates because the Apple updates
        may no longer be relevant.
        """
        try:
            os.unlink(self.apple_updates_plist)
        except (OSError, IOError):
            pass

    def DownloadAvailableUpdates(self):
        """Downloads the available updates using our local filtered sucatalog.

        Returns:
          Boolean. True if successful, False otherwise.
        """
        msg = 'Downloading available Apple Software Updates...'
        self._ResetMunkiStatusAndDisplayMessage(msg)

        # use our filtered local download catalog
        if not os.path.exists(self.local_download_catalog_path):
            munkicommon.display_error(
                'Missing local Software Update catalog at %s',
                self.local_download_catalog_path)
            return False

        catalog_url = 'file://localhost' + urllib2.quote(
            self.local_download_catalog_path)

        if munkicommon.getOsVersion() == '10.5':
            retcode = self._LeopardDownloadAvailableUpdates(catalog_url)
        else:
            retcode = self._RunSoftwareUpdate(
                ['--CatalogURL', catalog_url, '-d', '-a'],
                stop_allowed=True)

        if munkicommon.stopRequested():
            return False
        if retcode:  # there was an error
            munkicommon.display_error('softwareupdate error: %s' % retcode)
            return False
        return True

    def GetAvailableUpdateProductIDs(self):
        """Returns a list of product IDs of available Apple updates.

        Returns:
          A list of string Apple update products ids.
        """
        msg = 'Checking for available Apple Software Updates...'
        self._ResetMunkiStatusAndDisplayMessage(msg)

        try:  # remove any old ApplicableUpdates.plist, but ignore errors.
            os.unlink(self.applicable_updates_plist)
        except (OSError, IOError):
            pass

        # use our locally-cached Apple catalog
        catalog_url = 'file://localhost' + urllib2.quote(
            self.extracted_catalog_path)
        su_options = ['--CatalogURL', catalog_url, '-l', '-f',
                      self.applicable_updates_plist]

        retcode = self._RunSoftwareUpdate(su_options, stop_allowed=True)
        if munkicommon.stopRequested():
            return []
        if retcode:  # there was an error
            if munkicommon.getOsVersion() == '10.5':
                pass  # Leopard softwareupdate always returns a non-zero exit.
            else:
                munkicommon.display_error('softwareupdate error: %s' % retcode)
                return []

        try:
            pl_dict = FoundationPlist.readPlist(
                self.applicable_updates_plist)
        except FoundationPlist.NSPropertyListSerializationException:
            return []  # plist either doesn't exist or is malformed.

        if not pl_dict:
            return []

        results_array = pl_dict.get('phaseResultsArray', [])
        return [item['productKey'] for item in results_array
                if 'productKey' in item]

    def ExtractAndCopyDownloadedCatalog(self, _open=open):
        """Copy the downloaded catalog to a new file, extracting if gzipped.

        Args:
          _open: func, default builtin open(), open method for unit testing.
        """
        if not os.path.exists(self.local_catalog_dir):
            try:
                os.makedirs(self.local_catalog_dir)
            except OSError, oserr:
                raise ReplicationError(oserr)

        local_apple_sus_catalog = os.path.join(
            self.local_catalog_dir, APPLE_EXTRACTED_CATALOG_NAME)

        f = _open(self.apple_download_catalog_path, 'rb')
        magic = f.read(2)
        contents = ''
        if magic == '\x1f\x8b':  # File is gzip compressed.
            f.close()  # Close the open handle first.
            f = gzip.open(self.apple_download_catalog_path, 'rb')
        else:  # Hopefully a nice plain plist.
            f.seek(0)
        contents = f.read()
        f.close()
        f = _open(local_apple_sus_catalog, 'wb')
        f.write(contents)
        f.close()

    def _GetAppleCatalogURL(self):
        """Returns the catalog URL of the Apple SU catalog for the current Mac.

        Returns:
          String catalog URL for the current Mac.
        Raises:
          CatalogNotFoundError: an Apple catalog was not found for this Mac.
        """
        # Prefer Munki's preferences file.
        munkisuscatalog = munkicommon.pref('SoftwareUpdateServerURL')
        if munkisuscatalog:
            return munkisuscatalog

        # Otherwise prefer MCX or /Library/Preferences/com.apple.SoftwareUpdate
        prefs_catalog_url = self.GetSoftwareUpdatePref('CatalogURL')
        if prefs_catalog_url:
            return prefs_catalog_url

        # Finally, fall back to using a hard-coded url in DEFAULT_CATALOG_URLS.
        os_version = munkicommon.getOsVersion()
        catalog_url = DEFAULT_CATALOG_URLS.get(os_version, None)
        if catalog_url:
            return catalog_url

        raise CatalogNotFoundError(
            'No default Software Update CatalogURL for: %s' % os_version)

    def CacheAppleCatalog(self):
        """Caches a local copy of the current Apple SUS catalog.

        Raises:
          CatalogNotFoundError: a catalog was not found to cache.
          ReplicationError: there was an error making the cache directory.
          fetch.MunkiDownloadError: error downloading the catalog.
        """
        try:
            catalog_url = self._GetAppleCatalogURL()
        except CatalogNotFoundError, e:
            munkicommon.display_error(str(e))
            raise
        if not os.path.exists(self.temp_cache_dir):
            try:
                os.makedirs(self.temp_cache_dir)
            except OSError, oserr:
                raise ReplicationError(oserr)
        msg = 'Checking Apple Software Update catalog...'
        self._ResetMunkiStatusAndDisplayMessage(msg)
        munkicommon.display_detail('Caching CatalogURL %s', catalog_url)
        try:
            file_changed = self.GetSoftwareUpdateResource(
                catalog_url, self.apple_download_catalog_path, resume=True)
            self.ExtractAndCopyDownloadedCatalog()
        except fetch.MunkiDownloadError:
            raise

    def InstalledApplePackagesHaveChanged(self):
        """Generates a SHA-256 checksum of the info for all packages in the
        receipts database whose id matches com.apple.* and compares it to a
        stored version of this checksum.

        Returns:
          Boolean. False if the checksums match, True if they differ."""
        cmd = ['/usr/sbin/pkgutil', '--regexp', '-pkg-info-plist',
               r'com\.apple\.*']
        proc = subprocess.Popen(cmd, shell=False, bufsize=1,
                                stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        output, unused_err = proc.communicate()

        current_apple_packages_checksum = hashlib.sha256(output).hexdigest()
        old_apple_packages_checksum = munkicommon.pref(
            'InstalledApplePackagesChecksum')

        if current_apple_packages_checksum == old_apple_packages_checksum:
            return False
        else:
            munkicommon.set_pref('InstalledApplePackagesChecksum',
                                 current_apple_packages_checksum)
            return True

    def _IsForceCheckNeccessary(self, original_hash):
        """Returns True if a force check is needed, False otherwise.

        Args:
          original_hash: the SHA-256 hash of the Apple catalog before being
              redownloaded.
        Returns:
          Boolean. True if a force check is needed, False otherwise.
        """
        new_hash = munkicommon.getsha256hash(self.apple_download_catalog_path)
        if original_hash != new_hash:
            munkicommon.log('Apple update catalog has changed.')
            return True

        if self.InstalledApplePackagesHaveChanged():
            munkicommon.log('Installed Apple packages have changed.')
            return True

        if not self.AvailableUpdatesAreDownloaded():
            munkicommon.log('Downloaded updates do not match our list '
                            'of available updates.')
            return True

        return False

    def CheckForSoftwareUpdates(self, force_check=True):
        """Check if Apple Software Updates are available, if needed or forced.

        Args:
          force_check: Boolean. If True, forces a check, otherwise only checks
              if the last check is deemed outdated.
        Returns:
          Boolean. True if there are new updates, False otherwise.
        """
        before_hash = munkicommon.getsha256hash(
            self.apple_download_catalog_path)

        try:
            self.CacheAppleCatalog()
        except CatalogNotFoundError:
            return False
        except ReplicationError, err:
            munkicommon.display_warning(
                'Could not download Apple SUS catalog:')
            munkicommon.display_warning('\t%s', str(err))
            return False
        except fetch.MunkiDownloadError:
            munkicommon.display_warning(
                'Could not download Apple SUS catalog.')
            return False

        if not force_check and not self._IsForceCheckNeccessary(before_hash):
            munkicommon.display_info('Skipping Apple Software Update check '
                                     'because sucatalog is unchanged, installed Apple packages are '
                                     'unchanged and we recently did a full check.')
            return False

        product_ids = self.GetAvailableUpdateProductIDs()
        if not product_ids:
            # No updates found (not currently differentiating
            # "softwareupdate -l" failure from no updates found).
            munkicommon.set_pref('LastAppleSoftwareUpdateCheck', NSDate.date())
            return False

        self._WriteFilteredCatalog(product_ids, self.filtered_catalog_path)
        try:
            self.CacheUpdateMetadata()
        except ReplicationError, err:
            munkicommon.display_warning(
                'Could not replicate software update metadata:')
            munkicommon.display_warning('\t%s', str(err))
            return False
        if munkicommon.stopRequested():
            return False
        if self.DownloadAvailableUpdates():  # Success; ready to install.
            munkicommon.set_pref('LastAppleSoftwareUpdateCheck', NSDate.date())
            return True
        else:
            return False  # Download error, allow check again soon.

    def AvailableUpdatesAreDownloaded(self):
        """Verifies that applicable/available updates have been downloaded.

        Returns:
          Boolean. False if one or more product directories are missing, True
          otherwise (including when there are no available updates).
        """
        index_plist = '/Library/Updates/index.plist'
        apple_updates = self.GetSoftwareUpdateInfo()
        if not apple_updates:
            return True

        try:
            download_index = FoundationPlist.readPlist(index_plist)
            downloaded = download_index.get('ProductPaths', [])
        except FoundationPlist.FoundationPlistException:
            munkicommon.log(
                'Apple downloaded update index is invalid: %s' % index_plist)
            return False

        for update in apple_updates:
            product_id = update.get('productKey')
            if product_id:
                product_dir_exists = os.path.isdir(os.path.join(
                    '/Library/Updates', downloaded.get(product_id, '')))
                name = update['name']
                if product_id not in downloaded:
                    munkicommon.log(
                        'Apple Update product is not downloaded: %s' % name)
                    return False
                elif not product_dir_exists:
                    munkicommon.log(
                        'Apple Update product directory is missing: %s' % name)
                    return False
        return True

    def GetSoftwareUpdateInfo(self):
        """Uses AvailableUpdates.plist to generate the AppleUpdates.plist,
        which records available updates in the format that
        Managed Software Update.app expects.

        Returns:
          List of dictionary update data.
        """
        if not os.path.exists(self.applicable_updates_plist):
            return []  # no applicable_updates, so bail

        infoarray = []
        plist = FoundationPlist.readPlist(self.applicable_updates_plist)
        update_list = plist.get('phaseResultsArray', [])
        for update in update_list:
            iteminfo = {
                'description': update.get('description', ''),
                'name': update['ignoreKey'],
                'version_to_install': update['version'],
                'display_name': update['name'],
                'installed_size': update['sizeInKB'],
                'productKey': update['productKey']
            }
            blocking_apps = self.GetBlockingApps(update['productKey'])
            if blocking_apps:
                iteminfo['blocking_applications'] = blocking_apps
            if update.get('restartRequired') == 'YES':
                iteminfo['RestartAction'] = 'RequireRestart'
            infoarray.append(iteminfo)
        return infoarray

    def WriteAppleUpdatesFile(self):
        """Writes a file used by the MSU GUI to display available updates.

        Returns:
          Boolean. True if apple updates was updated, False otherwise.
        """
        apple_updates = self.GetSoftwareUpdateInfo()
        if apple_updates:
            plist = {'AppleUpdates': apple_updates}
            FoundationPlist.writePlist(plist, self.apple_updates_plist)
            return True
        else:
            try:
                os.unlink(self.apple_updates_plist)
            except (OSError, IOError):
                pass
            return False

    def DisplayAppleUpdateInfo(self):
        """Prints Apple update information and updates ManagedInstallReport."""
        try:
            pl_dict = FoundationPlist.readPlist(self.apple_updates_plist)
        except FoundationPlist.FoundationPlistException:
            munkicommon.display_error(
                'Error reading: %s', self.apple_updates_plist)
            return
        apple_updates = pl_dict.get('AppleUpdates', [])
        if apple_updates:
            munkicommon.report['AppleUpdates'] = apple_updates
            munkicommon.display_info(
                'The following Apple Software Updates are available to '
                'install:')
        for item in apple_updates:
            munkicommon.display_info(
                '    + %s-%s' % (
                    item.get('display_name', ''),
                    item.get('version_to_install', '')))
            if item.get('RestartAction') in self.RESTART_ACTIONS:
                munkicommon.display_info('       *Restart required')
                munkicommon.report['RestartRequired'] = True
            elif item.get('RestartAction') == 'RequireLogout':
                munkicommon.display_info('       *Logout required')
                munkicommon.report['LogoutRequired'] = True

    def GetSoftwareUpdatePref(self, pref_name):
        """Returns a preference from com.apple.SoftwareUpdate.

        Uses CoreFoundation.

        Args:
          pref_name: str preference name to get.
        """
        return CFPreferencesCopyAppValue(
            pref_name, APPLE_SOFTWARE_UPDATE_PREFS_DOMAIN)

    def _LeopardSetupSoftwareUpdateCheck(self):
        """Set defaults for root user and current host; needed for Leopard."""
        defaults = {
            'AgreedToLicenseAgreement': True,
            'AutomaticDownload': True,
            'LaunchAppInBackground': True,
        }
        for key, value in defaults.iteritems():
            CFPreferencesSetValue(
                key, value, APPLE_SOFTWARE_UPDATE_PREFS_DOMAIN,
                kCFPreferencesCurrentUser, kCFPreferencesCurrentHost)
        if not CFPreferencesAppSynchronize(APPLE_SOFTWARE_UPDATE_PREFS_DOMAIN):
            munkicommon.display_warning(
                'Error setting com.apple.SoftwareUpdate ByHost preferences.')

    def _LeopardDownloadAvailableUpdates(self, catalog_url):
        """Clunky process to download Apple updates in Leopard.

        Args:
          catalog_url: str catalog URL.
        """
        softwareupdateapp = '/System/Library/CoreServices/Software Update.app'
        softwareupdateappbin = os.path.join(
            softwareupdateapp, 'Contents/MacOS/Software Update')
        softwareupdatecheck = os.path.join(
            softwareupdateapp, 'Contents/Resources/SoftwareUpdateCheck')

        try:
            # record mode of Software Update.app executable
            rawmode = os.stat(softwareupdateappbin).st_mode
            oldmode = stat.S_IMODE(rawmode)
            # set mode of Software Update.app executable so it won't launch
            # yes, this is a hack.  So sue me.
            os.chmod(softwareupdateappbin, 0)
        except OSError, err:
            munkicommon.display_warning(
                'Error with os.stat(Softare Update.app): %s', str(err))
            munkicommon.display_warning('Skipping Apple SUS check.')
            return -2

        # Set SoftwareUpdateCheck to do things automatically
        self._LeopardSetupSoftwareUpdateCheck()
        # switch to our local filtered sucatalog
        # Using the NSDefaults Argument Domain described here:
        # http://developer.apple.com/library/mac/#documentation/
        #        Cocoa/Conceptual/UserDefaults/Concepts/DefaultsDomains.html
        cmd = [softwareupdatecheck, '-CatalogURL', catalog_url]
        try:
            # now check for updates
            proc = subprocess.Popen(cmd, shell=False, bufsize=1,
                                    stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT)
        except OSError, err:
            munkicommon.display_warning('Error with Popen(%s): %s', cmd, err)
            munkicommon.display_warning('Skipping Apple SUS check.')
            # safely revert the chmod from above.
            try:
                # put mode back for Software Update.app executable
                os.chmod(softwareupdateappbin, oldmode)
            except OSError:
                pass
            return -3

        while True:
            output = proc.stdout.readline().decode('UTF-8')
            if munkicommon.stopRequested():
                os.kill(proc.pid, 15)  # 15 is SIGTERM
                break
            if not output and (proc.poll() != None):
                break
            # send the output to STDOUT or MunkiStatus as applicable
            if output.rstrip() == '':
                continue
            # output from SoftwareUpdateCheck looks like this:
            # 2011-07-28 09:35:58.450 SoftwareUpdateCheck[598:10b]
            #   Downloading foo
            # We can pretty it up before display.
            fields = output.rstrip().split()
            if len(fields) > 3:
                munkicommon.display_status_minor(' '.join(fields[3:]))

        retcode = proc.poll()
        # there's always an error on Leopard
        # because we prevent the app from launching
        # so let's just ignore them
        retcode = 0
        # get SoftwareUpdate's LastResultCode
        last_result_code = self.GetSoftwareUpdatePref('LastResultCode') or 0
        if last_result_code > 2:
            retcode = last_result_code
        if retcode:  # retcode != 0, error
            munkicommon.display_error('softwareupdate error: %s' % retcode)

        try:
            # put mode back for Software Update.app executable
            os.chmod(softwareupdateappbin, oldmode)
        except OSError:
            pass

        return retcode

    def _RunSoftwareUpdate(
            self, options_list, stop_allowed=False, mode=None, results=None):
        """Runs /usr/sbin/softwareupdate with options.

        Provides user feedback via command line or MunkiStatus.

        Args:
          options_list: sequence of options to send to softwareupdate.
          stopped_allowed:
          mode:
          results:
        Returns:
          Integer softwareupdate exit code.
        """
        if results == None:
            # we're not interested in the results,
            # but need to create a temporary dict anyway
            results = {}

        # we need to wrap our call to /usr/sbin/softwareupdate with a utility
        # that makes softwareupdate think it is connected to a tty-like
        # device so its output is unbuffered so we can get progress info
        #
        # Try to find our ptyexec tool
        # first look in the parent directory of this file's directory
        # (../)
        parent_dir = os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))
        ptyexec_path = os.path.join(parent_dir, 'ptyexec')
        if not os.path.exists(ptyexec_path):
            # try absolute path in munki's normal install dir
            ptyexec_path = '/usr/local/munki/ptyexec'
        if os.path.exists(ptyexec_path):
            cmd = [ptyexec_path]
        else:
            # fall back to /usr/bin/script
            # this is not preferred because it uses way too much CPU
            # checking stdin for input that will never come...
            cmd = ['/usr/bin/script', '-q', '-t', '1', '/dev/null']
        cmd.append('/usr/sbin/softwareupdate')

        os_version_tuple = munkicommon.getOsVersion(as_tuple=True)
        if os_version_tuple > (10, 5):
            cmd.append('-v')

        cmd.extend(options_list)

        try:
            job = launchd.Job(cmd)
            job.start()
        except launchd.LaunchdJobException, err:
            munkicommon.display_warning(
                'Error with launchd job (%s): %s', cmd, str(err))
            munkicommon.display_warning('Skipping softwareupdate run.')
            return -3

        results['installed'] = []
        results['download'] = []

        last_output = None
        while True:
            if stop_allowed and munkicommon.stopRequested():
                job.stop()
                break

            output = job.stdout.readline()
            if not output:
                if job.returncode() is not None:
                    break
                else:
                    # no data, but we're still running
                    # sleep a bit before checking for more output
                    time.sleep(1)
                    continue

            # Don't bother parsing the stdout output if it hasn't changed since
            # the last loop iteration.
            if last_output == output:
                continue
            last_output = output

            output = output.decode('UTF-8').strip()
            # send the output to STDOUT or MunkiStatus as applicable
            if output.startswith('Progress: '):
                # Snow Leopard/Lion progress info with '-v' flag
                try:
                    percent = int(output[10:].rstrip('%'))
                except ValueError:
                    percent = -1
                munkicommon.display_percent_done(percent, 100)
            elif output.startswith('Software Update Tool'):
                # don't display this
                pass
            elif output.startswith('Copyright 2'):
                # don't display this
                pass
            elif output.startswith('Installing ') and mode == 'install':
                item = output[11:]
                if item:
                    self._ResetMunkiStatusAndDisplayMessage(output)
            elif output.startswith('Installed '):
                # 10.6 / 10.7. Successful install of package name.
                if mode == 'install':
                    munkicommon.display_status_minor(output)
                    results['installed'].append(output[10:])
                else:
                    pass
                    # don't display.
                    # softwareupdate logging "Installed" at the end of a
                    # successful download-only session is odd.
            elif output.startswith('Done '):
                # 10.5. Successful install of package name.
                munkicommon.display_status_minor(output)
                results['installed'].append(output[5:])
            elif output.startswith('Downloading ') and mode == 'install':
                # This is 10.5 & 10.7 behavior for a missing subpackage.
                munkicommon.display_warning(
                    'A necessary subpackage is not available on disk '
                    'during an Apple Software Update installation '
                    'run: %s' % output)
                results['download'].append(output[12:])
            elif output.startswith('Package failed:'):
                # Doesn't tell us which package.
                munkicommon.display_error(
                    'Apple update failed to install: %s' % output)
            elif output.startswith('x '):
                # don't display this, it's just confusing
                pass
            elif 'Missing bundle identifier' in output:
                # don't display this, it's noise
                pass
            elif output == '':
                pass
            elif os_version_tuple == (10, 5) and output[0] in '.012468':
                # Leopard: See if there is percent-done info we can use,
                # which will look something like '.20..' or '0..' or '.40...60.'
                # so strip '.' chars and grab the last set of numbers
                output = output.strip('.').split('.')[-1]
                try:
                    percent = int(output)
                    if percent in [0, 20, 40, 60, 80, 100]:
                        munkicommon.display_percent_done(percent, 100)
                except ValueError:
                    pass
            else:
                munkicommon.display_status_minor(output)

        retcode = job.returncode()
        if retcode == 0:
            # get SoftwareUpdate's LastResultCode
            last_result_code = self.GetSoftwareUpdatePref(
                'LastResultCode') or 0
            if last_result_code > 2:
                retcode = last_result_code

    # TODO(jrand): The below functions are externally called. Should all
    #   others be private?

    def InstallAppleUpdates(self):
        """Uses softwareupdate to install previously downloaded updates.

        Returns:
          Boolean. True if a restart is needed after install, False otherwise.
        """
        # disable Stop button if we are presenting GUI status
        if munkicommon.munkistatusoutput:
            munkistatus.hideStopButton()

        msg = 'Installing available Apple Software Updates...'
        self._ResetMunkiStatusAndDisplayMessage(msg)

        restartneeded = self.IsRestartNeeded()
        # use our filtered local catalog
        if not os.path.exists(self.local_catalog_path):
            munkicommon.display_error(
                'Missing local Software Update catalog at %s',
                self.local_catalog_path)
            return False  # didn't do anything, so no restart needed

        installlist = self.GetSoftwareUpdateInfo()
        installresults = {'installed': [], 'download': []}

        catalog_url = 'file://localhost' + urllib2.quote(
            self.local_catalog_path)
        retcode = self._RunSoftwareUpdate(
            ['--CatalogURL', catalog_url, '-i', '-a'],
            mode='install', results=installresults)

        if not 'InstallResults' in munkicommon.report:
            munkicommon.report['InstallResults'] = []

        for item in installlist:
            rep = {}
            rep['name'] = item.get('display_name')
            rep['version'] = item.get('version_to_install', '')
            rep['applesus'] = True
            rep['time'] = NSDate.new()
            rep['productKey'] = item.get('productKey', '')
            message = 'Apple Software Update install of %s-%s: %s'
            if rep['name'] in installresults['installed']:
                rep['status'] = 0
                install_status = 'SUCCESSFUL'
            elif rep['name'] in installresults['download']:
                rep['status'] = -1
                install_status = 'FAILED due to missing package.'
                munkicommon.display_warning(
                    'Apple update %s, %s failed. A sub-package was missing '
                    'on disk at time of install.'
                    % (rep['name'], rep['productKey']))
            else:
                rep['status'] = -2
                install_status = 'FAILED for unknown reason'
                munkicommon.display_warning(
                    'Apple update %s, %s failed to install. No record of '
                    'success or failure.' % (rep['name'], rep['productKey']))

            munkicommon.report['InstallResults'].append(rep)
            log_msg = message % (rep['name'], rep['version'], install_status)
            munkicommon.log(log_msg, 'Install.log')

        if retcode:  # there was an error
            munkicommon.display_error('softwareupdate error: %s' % retcode)
        # clean up our now stale local cache
        if os.path.exists(self.cache_dir):
            # TODO(unassigned): change this to Pythonic delete.
            unused_retcode = subprocess.call(
                ['/bin/rm', '-rf', self.cache_dir])
        # remove the now invalid AppleUpdates.plist file.
        try:
            os.unlink(self.apple_updates_plist)
        except OSError:
            pass
        # Also clear our pref value for last check date. We may have
        # just installed an update which is a pre-req for some other update.
        # Let's check again soon.
        munkicommon.set_pref('LastAppleSoftwareUpdateCheck', None)

        # show stop button again
        if munkicommon.munkistatusoutput:
            munkistatus.showStopButton()

        return restartneeded

    def AppleSoftwareUpdatesAvailable(
            self, force_check=False, suppress_check=False):
        """Checks for available Apple Software Updates, trying not to hit the
        SUS more than needed.

        Args:
          force_check: Boolean. If True, forces a softwareupdate run.
          suppress_check: Boolean. If True, skips a softwareupdate run.
        Returns:
          Boolean. True if Apple updates are available, False otherwise.
        """
        if suppress_check:
            # typically because we're doing a logout install; if
            # there are no waiting Apple Updates we shouldn't
            # trigger a check for them.
            pass
        elif force_check:
            # typically because user initiated the check from
            # Managed Software Update.app
            self.CheckForSoftwareUpdates(force_check=True)
        else:
            # have we checked recently?  Don't want to check with
            # Apple Software Update server too frequently
            now = NSDate.new()
            next_su_check = now
            last_su_check_string = munkicommon.pref(
                'LastAppleSoftwareUpdateCheck')
            if last_su_check_string:
                try:
                    last_su_check = NSDate.dateWithString_(
                        last_su_check_string)
                    # dateWithString_ returns None if invalid date string.
                    if not last_su_check:
                        raise ValueError
                    interval = 24 * 60 * 60  # only force check every 24 hours.
                    next_su_check = last_su_check.dateByAddingTimeInterval_(
                        interval)
                except (ValueError, TypeError):
                    pass
            if now.timeIntervalSinceDate_(next_su_check) >= 0:
                self.CheckForSoftwareUpdates(force_check=True)
            else:
                self.CheckForSoftwareUpdates(force_check=False)
        if munkicommon.stopRequested():
            return False
        if self.WriteAppleUpdatesFile():
            self.DisplayAppleUpdateInfo()
            return True
        else:
            return False

    def SoftwareUpdateList(self):
        """Returns a list of str update names using softwareupdate -l."""
        if self._update_list_cache is not None:
            return self._update_list_cache

        updates = []
        munkicommon.display_detail(
            'Getting list of available Apple Software Updates')
        cmd = ['/usr/sbin/softwareupdate', '-l']
        proc = subprocess.Popen(cmd, shell=False, bufsize=1,
                                stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        output, unused_err = proc.communicate()
        if proc.returncode == 0:
            updates = [str(item)[5:] for item in str(output).splitlines()
                       if str(item).startswith('   * ')]
        munkicommon.display_detail(
            'softwareupdate returned %d updates.' % len(updates))
        self._update_list_cache = updates
        return updates


# Make the new appleupdates module easily dropped in with exposed funcs for now.
apple_updates_object = None


def getAppleUpdatesInstance():
    """Returns either an AppleUpdates instance, either cached or new."""
    global apple_updates_object
    if apple_updates_object is None:
        apple_updates_object = AppleUpdates()
    return apple_updates_object


def softwareUpdateList():
    """Method for drop-in appleupdates replacement; see primary method docs."""
    return getAppleUpdatesInstance().SoftwareUpdateList()


def clearAppleUpdateInfo():
    """Method for drop-in appleupdates replacement; see primary method docs."""
    return getAppleUpdatesInstance().ClearAppleUpdateInfo()


def installAppleUpdates():
    """Method for drop-in appleupdates replacement; see primary method docs."""
    return getAppleUpdatesInstance().InstallAppleUpdates()


def appleSoftwareUpdatesAvailable(forcecheck=False, suppresscheck=False):
    """Method for drop-in appleupdates replacement; see primary method docs."""
    return getAppleUpdatesInstance().AppleSoftwareUpdatesAvailable(
        force_check=forcecheck, suppress_check=suppresscheck)
