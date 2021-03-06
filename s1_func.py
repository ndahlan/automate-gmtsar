#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Wed Oct 25 11:45:24 2017

@author: elindsey
"""

import os,sys,shutil,glob,datetime,multiprocessing,random,string #,subprocess,errno
import requests,json,tarfile

#user imports 
#note that this is a circular import
import gmtsar_func

######################## Sentinel-specific functions ########################
# This satellite is a real nightmare for my attempts at standardization 

def find_scenes_s1(s1_subswath,s1_orbit_dirs):
    """
    For Sentinel, data.in is a reference table that links the xml file with the correct orbit file.
    """
    outputlist=[]
    list_of_images=glob.glob('raw_orig/S1*.SAFE/annotation/s1?-iw%s-slc-vv*.xml'%s1_subswath)
    #print('found list')
    #print(list_of_images)

    for item in list_of_images:
        #make sure we have just the file basename
        xml_name = os.path.basename(item)
        #find latest EOF and create the "long-format" image name containing the EOF
        image_name,eof_name = get_s1_image_and_orbit(xml_name,s1_orbit_dirs)
        # copy this EOF to the raw_orig directory
        command = 'cp %s raw_orig/'%(eof_name)
        gmtsar_func.run_command(command, logging=False)
        # append image to list
        outputlist.append(image_name)
    return outputlist

def unzip_images_to_dir(dirlist,unzip_dir,nproc=1):
    """
    Look for zipped S1 SLC files and unzip them to a temporary directory, optionally running in parallel.
    """
    images_list=[]
    for searchdir in dirlist:
        dir_list=glob.glob('%s/S1?_IW_SLC*.zip'%searchdir)
        #print('in:',searchdir)
        #print('found list:',dir_list,'\n')
        images_list.extend(dir_list)
    #print('complete list:',images_list,'\n')

    cmds=[]
    for image in images_list:
        item=os.path.abspath(image)
        file=os.path.basename(item)
        cmd = 'unzip %s log_%s.txt'%(item,file)
        cmds.append(cmd)

    # go to the output directory so the unzipped results will fall there:
    owd=os.getcwd()
    os.chdir(unzip_dir)
    # run multiple unzip commands at the same time:
    with multiprocessing.Pool(processes=nproc) as pool:
        pool.map(gmtsar_func.run_command, cmds)
    os.chdir(owd)
    

def find_images_by_orbit(dirlist,s1_orbit_dirs,ftype='SAFE'):
    """
    For each Sentinel-1 satellite, find all images that were acquired on the same orbit, and order them by time
    """
    valid_modes = ['IW_SLC'] #we match only IW_SLC data
    
    #dictionaries will keep track of the results
    names       = dict()
    start_times = dict()
    eofs        = dict()
    
    for searchdir in dirlist:
        list_of_images=glob.glob('%s/S1*%s'%(searchdir,ftype))
        #print('in',searchdir)
        #print('found list',list_of_images,'\n')
        
        for item in list_of_images:
            # we want the full path and also just the file name
            item=os.path.abspath(item)
            file=os.path.basename(item)
            
            # get A/B, sat. mode, dates, orbit ID from the file name
            [sat_ab,sat_mode,image_start,image_end,orbit_num] = parse_s1_SAFE_name(file)
            #print('Read file:',file,'parameters:',sat_ab,sat_mode,image_start,image_end,orbit_num)

            if sat_mode in valid_modes:
                #Find matching EOF
                eof_name = get_latest_orbit_file(sat_ab,image_start,image_end,s1_orbit_dirs,skip_notfound=True)
                if eof_name is not None:
                    #print('Got EOF:',eof_name)
                
                    #add A or B to the orbit number and use as the unique ID for identifying orbits
                    ab_orbit='S1%s_%06d'%(sat_ab,orbit_num)
                    if ab_orbit not in names:
                        names[ab_orbit] = []
                        start_times[ab_orbit] = []
                        eofs[ab_orbit] = eof_name
                    elif eof_name != eofs[ab_orbit]:
                        #we've already found one scene from this orbit. check that it matches the same orbit file 
                        print('Error: found two scenes from same orbit number matching different EOFs. Check your data.')
                        sys.exit(1)
                    
                    #keep the images in time order. Find the index of the first time that is later than the image's time
                    timeindx=0
                    for starttime in start_times[ab_orbit]:
                        if starttime < image_start:
                            timeindx+=1
                    names[ab_orbit].insert(timeindx,item)
                    start_times[ab_orbit].insert(timeindx,image_start)
                    #print('Added image at position %d for orbit %s'%(timeindx,ab_orbit) )
                    #print('List is now %s'%names[ab_orbit])
            #print('')
    return names, eofs

def get_s1_auxfile(sat_ID, s1_orbit_dirs, force_update=False):
    # search for the aux cal file in orbit folders. If not found, 
    # or if force_update = True, download the latest version
    # from ESA to the first folder given. return full path to the file.

    aux_name=sat_ID.lower() + '-aux-cal.xml' # e.g. 's1a-aux-cal.xml'
    local_auxfile = None

    #first check for  the existence of a local version
    for s1_orbit_dir in s1_orbit_dirs:
        testfile = os.path.join(s1_orbit_dir,aux_name)
        if os.path.isfile(testfile):
            if force_update:
                # delete the file and leave local_auxfile=None alone.
                # This will cause the block below to trigger a download.
                os.remove(testfile)
            else:
                #found the file, we simply return its location
                local_auxfile=testfile
                break

    # if none found, download from ESA
    if local_auxfile is None:
        local_auxfile = get_latest_auxcal_esa_api(sat_ID.upper(),target_path=s1_orbit_dirs[0])

    return local_auxfile

def get_latest_auxcal_esa_api(sat_ID, target_path='.'):
    '''Query ESA for the latest aux file for this satellite and download it.
    Then extract the xml file and place it in the current directory.'''
    
    # query ESA for the latest AUX_CAL file
    params = {'product_type': 'AUX_CAL', 'product_name__startswith': sat_ID, 'ordering': '-creation_date', 'page_size': '1'}
    response = requests.get(url='https://qc.sentinel1.eo.esa.int/api/v1/', params=params)
    response.raise_for_status()
    qc_data = response.json()

    if qc_data['results']:
        # convert json to dictionary
        auxfile = qc_data['results'][0]

        # get remote url and download the file
        remote_url = auxfile['remote_url']
        local_filename = os.path.join(target_path,remote_url.split('/')[-1])
        response = requests.get(url=remote_url)
        response.raise_for_status()
        with open(local_filename, 'wb') as f:
            f.write(response.content)
        print('downloaded from ESA:',local_filename)
        
        # extract the compressed SAFE folder and place the aux_cal file in current directory.
        with tarfile.open(local_filename) as tf:
            members=tf.getmembers()
            for member in members:
                if '-aux-cal.xml' in member.name:
                    # strip the directory structure out of the name before extracting
                    member.name=os.path.basename(member.name)
                    tf.extract(member,path=target_path)
                    auxfile_name = os.path.join(target_path,member.name)
                    break
        print('extracted file:',auxfile_name)
    return auxfile_name

def get_latest_orbit_esa_api(sat_ab,start_time,end_time,orbit_type):
    """
    Use the ESA API to find the latest orbit file.
    Input example: 'A', '20180810T224719', '20180810T224816', 'AUX_POEORB'
    Returns a python dictionary, with important elements 'product_type', 'physical_name', and 'remote_url'.
    """
    # original credit to https://github.com/asjohnston-asf/s1qc-orbit-api/blob/master/src/main.py
    # modified by E. Lindsey, May 2020

    platform = f'S1{sat_ab}'

    params = {
        'product_type': orbit_type,
        'product_name__startswith': platform,
        'validity_start__lt': start_time,
        'validity_stop__gt': end_time,
        'ordering': '-creation_date',
        'page_size': '1',
    }

    response = requests.get(url='https://qc.sentinel1.eo.esa.int/api/v1/', params=params)
    response.raise_for_status()
    qc_data = response.json()

    orbit = None
    if qc_data['results']:
        orbit = qc_data['results'][0]
    return orbit

def get_latest_orbit_file(sat_ab,imagestart,imageend,s1_orbit_dirs,download_missing=True,skip_notfound=True):
    """
    Orbit files have 3 dates: production date, start and end range. Image files have 2 dates: start, end.
    We want to find the latest file (most recent production) whose range includes the range of the image.
    If none is found, look for the file from ESA by default.
    Return string includes absolute path to file.
    """
    eoflist=[]
    eofprodlist=[]
    latest_eof=None

    # add one hour to the image start/end times to ensure we have enough coverage in the orbit file
    imagestart_pad = imagestart - datetime.timedelta(hours=0.5)
    imageend_pad = imageend + datetime.timedelta(hours=0.5)
     
    for s1_orbit_dir in s1_orbit_dirs:
        for item in glob.glob(s1_orbit_dir+"/S1"+sat_ab+"*.EOF"):
            #get basename, and read dates from string
            eof=os.path.basename(item)
            [eofprod,eofstart,eofend] = get_dates_from_eof(eof)
            #check if the EOF validity dates span the entire image
            if eofstart < imagestart_pad and eofend > imageend_pad:
                eoflist.append(os.path.abspath(os.path.join(s1_orbit_dir,eof)))
                #record the production time to ensure we get the most recent one
                eofprodlist.append(eofprod)       
    if eoflist:
        #get the most recently produced valid EOF
        latest_eof = eoflist[eofprodlist.index(max(eofprodlist))]

    elif download_missing:
        #no EOF found locally, download from ESA
        print('No matching orbit file found locally, downloading from ESA')
        tstart=imagestart_pad.strftime('%Y%m%dT%H%M%S')
        tend=imageend_pad.strftime('%Y%m%dT%H%M%S')

        orbit = get_latest_orbit_esa_api(sat_ab,tstart,tend,'AUX_POEORB')
        if not orbit:
            orbit = get_latest_orbit_esa_api(sat_ab,tstart,tend,'AUX_RESORB')        

        if orbit:
            # set download location to one of the user-supplied folders. We assume the user would either supply one folder, or two folders with 'resorb' coming second.
            if len(s1_orbit_dirs)>1 and orbit['product_type'] == 'AUX_RESORB':
                target_dir=s1_orbit_dirs[1]
            else:
                target_dir=s1_orbit_dirs[0]

            # compute the full path to the file
            latest_eof = os.path.abspath(os.path.join(target_dir,orbit['physical_name']))

            # download the file
            remote_url = orbit['remote_url']
            response = requests.get(url=remote_url)
            response.raise_for_status()
            with open(latest_eof, 'wb') as f:
                f.write(response.content)

    if not latest_eof or not os.path.exists(latest_eof):
        if skip_notfound:
            print("Warning: No matching orbit file found for Sentinel-1%s during time %s to %s in %s - skipping"%(sat_ab,imagestart_pad,imageend_pad,s1_orbit_dirs))
            return None
        else:
            print("Error: No matching orbit file found for Sentinel-1%s during time %s to %s in %s"%(sat_ab,imagestart_pad,imageend_pad,s1_orbit_dirs))
            sys.exit(1)
            
    return latest_eof


def parse_s1_SAFE_name(safe_name):
    """
    SAFE file has name like S1A_IW_SLC__1SDV_20150224T114043_20150224T114111_004764_005E86_AD02.SAFE (or .zip)
    Function returns a list of 2 strings, 2 datetime objects and one integer ['A', 'IW_SLC', '20150224T114043','20150224T114111', 004764]
    """
    #make sure we have just the file name
    safe_name = os.path.basename(safe_name)
    #extract string components and convert to datetime objects
    sat_ab   = safe_name[2]
    sat_mode = safe_name[4:10]
    date1    = datetime.datetime.strptime(safe_name[17:32],'%Y%m%dT%H%M%S')
    date2    = datetime.datetime.strptime(safe_name[33:48],'%Y%m%dT%H%M%S')
    orbit_num= int(safe_name[49:55])
    return [sat_ab, sat_mode, date1, date2, orbit_num]


def get_datestring_from_xml(xml_name):
    """
    xml file has name like s1a-iw1-slc-vv-20150121t134413-20150121t134424-004270-005317-001.xml
    We want to return 20150121. 
    """
    #make sure we have just the file basename
    xml_name = os.path.basename(xml_name)
    #parse string by fixed format
    mydate=xml_name[15:23]
    return mydate


def get_date_range_from_xml(xml_name):
    """
    xml file has name like s1a-iw1-slc-vv-20150121t134413-20150121t134424-004270-005317-001.xml
    Function returns a list of 2 datetime objects matching the strings ['20150121T134413','20150121T134424']
    """
    #make sure we have just the file basename
    xml_name = os.path.basename(xml_name)
    #parse string by fixed format
    date1=datetime.datetime.strptime(xml_name[15:30],'%Y%m%dt%H%M%S')
    date2=datetime.datetime.strptime(xml_name[31:46],'%Y%m%dt%H%M%S')
    return [date1,date2]


def get_dates_from_eof(eof_name):
    """
    EOF file has name like S1A_OPER_AUX_POEORB_OPOD_20170917T121538_V20170827T225942_20170829T005942.EOF
    Function returns a list of 3 datetime objects matching the strings ['20170917T121538','20170827T225942','20170829T005942']
    """
    #make sure we have just the file basename
    eof_name = os.path.basename(eof_name)
    #parse string by fixed format
    date1=datetime.datetime.strptime(eof_name[25:40],'%Y%m%dT%H%M%S')
    date2=datetime.datetime.strptime(eof_name[42:57],'%Y%m%dT%H%M%S')
    date3=datetime.datetime.strptime(eof_name[58:73],'%Y%m%dT%H%M%S')
    return [date1,date2,date3]


def get_s1_image_and_orbit(xml_name,s1_orbit_dirs):
    #parse string by fixed format
    sat_ab=xml_name[2].upper()
    [imagestart,imageend]=get_date_range_from_xml(xml_name)
    eof_name = get_latest_orbit_file(sat_ab,imagestart,imageend,s1_orbit_dirs,skip_notfound=False);
    eof_basename = os.path.basename(eof_name)
    image_name = xml_name[:-4]+":"+eof_basename
    return image_name,eof_name


def write_ll_pins(fname, lons, lats, asc_desc):
    """
    Put lat/lon pairs in time order and write to a file.
    Lower latitude value comes first for 'A', while for 'D' higher lat. is first
    """
    if (asc_desc == 'D' and lats[0] < lats[1]) or (asc_desc == 'A' and lats[0] > lats[1]):
        lats.reverse()
        lons.reverse()
    lonlats=['%f %f'%(i,j) for i,j in zip(lons,lats)]
    gmtsar_func.write_list(fname,lonlats)


def create_frame_tops_parallel(safelist,eof,llpins,logfile,workdir):
    """
    Run the GMTSAR command create_frame_tops.csh to combine bursts within the given latitude bounds
    Modified version enables running in parallel by running in a temporary subdirectory.
    """

    # to run in parallel, we have to do everything inside a unique directory
    # this is because GMTSAR uses constant temp filenames that will collide with each other
    # in this case, the colliding name is the temp folder 'new.SAFE'
    os.makedirs(workdir, exist_ok=False)
    oldcwd=os.getcwd()
    os.chdir(workdir)

    # write file list to the current directory
    gmtsar_func.write_list('SAFE.list', safelist)

    # copy orbit file to the current directory, required for create_frame_tops.csh
    shutil.copy2(eof,os.getcwd())
    local_eof=os.path.basename(eof)

    # copy llpins file to current directory
    shutil.copy2(os.path.join(oldcwd,llpins),llpins)

    # create GMTSAR command and run it
    #cmd = '/home/share/insarscripts/automate/gmtsar_functions/create_frame_tops.csh SAFE.list %s %s 1 %s'%(local_eof, llpins, logfile)
    cmd = 'create_frame_tops.csh SAFE.list %s %s 1 %s'%(local_eof, llpins, logfile)
    gmtsar_func.run_command(cmd,logging=True)

    # copy result back to main directory
    result_safe=glob.glob('S1*SAFE')[0]
    shutil.move(result_safe,os.path.join(oldcwd,result_safe))
    shutil.move(logfile,oldcwd)

    # clean up
    os.chdir(oldcwd)
    shutil.rmtree(workdir)

def create_frame_tops(safelist,eof,llpins,logfile):
    """
    Run the GMTSAR command create_frame_tops.csh to combine bursts within the given latitude bounds
    Note, this command cannnot be run in parallel. Use 'create_frame_tops_parallel' instead.
    """
    # copy orbit file to the current directory, required for create_frame_tops.csh
    #print(eof)
    shutil.copy2(eof,os.getcwd())
    local_eof=os.path.basename(eof)

    cmd = '/home/share/insarscripts/automate/gmtsar_functions/create_frame_tops.csh %s %s %s 1 %s'%(safelist, local_eof, llpins, logfile)
    #cmd = '~/Dropbox/code/geodesy/insarscripts/automate/gmtsar_functions/create_frame_tops.csh %s %s %s 1 %s'%(safelist, local_eof, llpins, logfile)
    gmtsar_func.run_command(cmd,logging=True)

