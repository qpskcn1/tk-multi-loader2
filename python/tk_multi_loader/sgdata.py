# Copyright (c) 2013 Shotgun Software Inc.
# 
# CONFIDENTIAL AND PROPRIETARY
# 
# This work is provided "AS IS" and subject to the Shotgun Pipeline Toolkit 
# Source Code License included in this distribution package. See LICENSE.
# By accessing, using, copying or modifying this work you indicate your 
# agreement to the Shotgun Pipeline Toolkit Source Code License. All rights 
# not expressly granted therein are reserved by Shotgun Software Inc.

import os
import urllib
import tank
import uuid
import sys
import urlparse
import os
import urllib
import shutil


from tank.platform.qt import QtCore, QtGui

class ShotgunAsyncDataRetriever(QtCore.QThread):
    """
    Background worker class
    """    
    work_completed = QtCore.Signal(str, dict)
    work_failure = QtCore.Signal(str, str)
    
    def __init__(self, parent=None):
        """
        Construction
        """
        QtCore.QThread.__init__(self, parent)
        
        self._app = tank.platform.current_bundle()
        self._execute_tasks = True
        self._wait_condition = QtCore.QWaitCondition()
        self._queue_mutex = QtCore.QMutex()
        self._queue = []
        
    def clear(self):
        """
        Clear the queue
        """
        self._queue_mutex.lock()
        try:
            self._app.log_debug("Discarding %s items in sg queue..." % len(self._queue))
            self._queue = []
        finally:
            self._queue_mutex.unlock()
        
    def stop(self):
        """
        Stops the worker, run this before shutdown
        """
        self._execute_tasks = False
        self._wait_condition.wakeAll()
        self.wait()        
        
    def execute_find(self, entity_type, filters, fields, order = None):    
        """
        Run a shotgun find
        """
        uid = uuid.uuid4().hex
        
        work = {"id": uid, 
                "type": "find", 
                "entity_type": entity_type, 
                "filters": filters, 
                "fields": fields,
                "order": order }
        self._queue_mutex.lock()
        try:
            # first in the queue
            self._queue.insert(0, work)
        finally:
            self._queue_mutex.unlock()
            
        # wake up execution loop!
        self._wait_condition.wakeAll()
        
        return uid
        
        
    def download_thumbnail(self, url, entity_type, entity_id):
        """
        Downloads a thumbnail from the internet
        """

        uid = uuid.uuid4().hex
        
        work = {"id": uid, 
                "type": "thumbnail", 
                "url": url,
                "entity_type": entity_type,
                "entity_id": entity_id }
        self._queue_mutex.lock()
        try:
            # first in the queue - this way thumbnails that already exist
            # cached on disk will load quickly and downloaded thumbs will
            # always load as a low priority thing
            self._queue.insert(0, work)
        finally:
            self._queue_mutex.unlock()
            
        # wake up execution loop!
        self._wait_condition.wakeAll()
        
        return uid

    def _get_thumbnail_path(self, url, entity_id, entity_type):
        """
        Returns the location on disk suitable for a thumbnail given its metadata
        """

        # establish the root path        
        cache_path_items = [self._app.cache_location, "thumbnails", entity_type]
        
        # the S3 urls are not suitable as cache keys so use type/id
        # split the number into chunks and preceed with type
        # 12345 --> ['1','2','3','4','5']
        cache_path_items.extend(list(str(entity_id)))
        
        # and append a file name. Assume we always get a jpeg back from sg
        cache_path_items.append("%s.jpg" % entity_id)
        
        # join up the path
        path_to_cached_thumb = os.path.join(*cache_path_items)
        
        return path_to_cached_thumb



    ############################################################################################
    # async stuff



    def run(self):

        #############################################
        # keep running until stop() is being called
        while self._execute_tasks:
            
            
            #########################################
            # Step 1. get the next item to process. 
            item_to_process = None
            self._queue_mutex.lock()
            try:
                if len(self._queue) == 0:
                    
                    # wait for some more work - this unlocks the mutex
                    # until the wait condition is signalled where it
                    # will then attempt to obtain a lock before returning
                    self._wait_condition.wait(self._queue_mutex)
                    
                    if len(self._queue) == 0:
                        # still nothing in the queue!
                        continue
                
                # take the first item in the queue
                item_to_process = self._queue.pop(0)
            finally:
                self._queue_mutex.unlock()



            ##############################################
            # Step 2. Process next item and send signals. 
            data = None
            try:
                # process the item:
                if item_to_process["type"] == "find":
                    
                    sg = self._app.shotgun.find(item_to_process["entity_type"],
                                                  item_to_process["filters"],
                                                  item_to_process["fields"],
                                                  item_to_process["order"])
                    # need to wrap it in a dict not to confuse pyqts signals and type system
                    data = {"sg": sg}
                
                elif item_to_process["type"] == "thumbnail":
                    
                    url = item_to_process["url"]                    
                    entity_id = item_to_process["entity_id"]
                    entity_type = item_to_process["entity_type"]
                    path_to_cached_thumb = self._get_thumbnail_path(url, entity_id, entity_type)
                    
                    if not os.path.exists(path_to_cached_thumb):
                        # no cached thumb yet. Re-queue this task, this time
                        # at the back of the queue (the slow end of the queue)
                        # give it a new status to indicate that we should download
                        item_to_process["type"] = "thumbnail_download"
                        self._queue_mutex.lock()
                        try:
                            # back of the queue
                            self._queue.append(item_to_process)
                        finally:
                            self._queue_mutex.unlock()
                            
                        # note that we are not setting the data variable to anything here,
                        # so no signal will be sent.
                        
                    else:
                        # we have a path on disk!
                        data = {"thumb_path": path_to_cached_thumb }
                
                
                elif item_to_process["type"] == "thumbnail_download":
                    
                    url = item_to_process["url"]
                    entity_id = item_to_process["entity_id"]
                    entity_type = item_to_process["entity_type"]
                    path_to_cached_thumb = self._get_thumbnail_path(url, entity_id, entity_type)
                    
                    try:
                        (temp_file, _) = urllib.urlretrieve(url)
                    except Exception, e:
                        raise Exception("Could not download data from the url '%s'. Error: %s" % (url, e))
            
                    # now try to cache it
                    try:
                        self._app.ensure_folder_exists(os.path.dirname(path_to_cached_thumb))
                        shutil.copy(temp_file, path_to_cached_thumb)
                        # as a tmp file downloaded by urlretrieve, permissions are super strict
                        # modify the permissions of the file so it's writeable by others
                        os.chmod(path_to_cached_thumb, 0666)            
                    except Exception, e:
                        raise Exception("Could not cache thumbnail %s in %s. "
                                        "Error: %s" % (url, path_to_cached_thumb, e))
             
                    data = {"thumb_path": path_to_cached_thumb }
                    
                
            except Exception, e:
                if self._execute_tasks:
                    self.work_failure.emit(item_to_process["id"], "An error occured: %s" % e)
            else:
                if self._execute_tasks and data:
                    self.work_completed.emit(item_to_process["id"], data)
                