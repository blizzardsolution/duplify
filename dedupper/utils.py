#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Sat May 19 17:53:34 2018

@author: jachi
"""
import datetime
import logging
import string
from collections import defaultdict
from functools import reduce
from gc import collect
from io import StringIO
from itertools import chain
from operator import itemgetter
from random import *
from time import perf_counter

import numpy as np
import pandas as pd
from django import db
from django.conf import settings
from django.db.models import Avg
from fuzzyset import FuzzySet
from fuzzywuzzy import fuzz
from range_key_dict import RangeKeyDict
from recordlinkage.preprocessing import clean, phonenumbers
from simple_salesforce import Salesforce
from tablib import Dataset

import dedupper.threads
from dedupper.models import repContact, sfcontact, dedupTime, uploadTime, progress
from dedupper.resources import RepContactResource, SFContactResource

#find more on fuzzywuzzy at https://github.com/seatgeek/fuzzywuzzy
standard_sorting_range = RangeKeyDict({
    (97, 101): 'Duplicate',
    (95, 97): 'Manual Check',
    (0, 95): 'Undecided'
})
last_key_sorting_range = RangeKeyDict({
    (97, 101): 'Duplicate',
    (95, 97): 'Manual Check',
    (0, 95): 'New Record'
})
manual_sorting_range = RangeKeyDict({
    (95, 101): 'Manual Check',
    (0, 95): 'Undecided',
})
last_manual_sorting_range = RangeKeyDict({
    (95, 101): 'Manual Check',
    (0, 95): 'New Record'
})
loaded = False
times = []
waiting= True
keylist = list()
reps_df = sf_df = currKey = sort_alg = key_stats = None
start=end=cnt=doneKeys=totalKeys=0
done=complete= False
rep_key_map = None
# TODO finish phone/email multi sf field mapping

#TODO add docstrings go to realpython.com/documenting-python-code/
#TODO update documentation go to dbader.org/blog/write-a-great-readme-for-your-github-project
#TODO django test cases realpython <--

#simple csv to dataframe
def convert_csv(file, name):
    if  'csv' in name:
        print('converting CSV: ', str(file))
        pd_csv = pd.read_csv(file, encoding = "cp1252", delimiter=',', dtype=str)  #western european
    else:
        print('converting excel sheet:', str(file))
        pd_csv = pd.read_excel(file)  # western european


    return list(pd_csv), pd_csv

def find_rep_dups(rep, keys, numthreads):
    global cnt
    print ('rep dedupe') #track the number of attempted dedups
    multi_key = False                   #for tracking the one to many key generation of keys containing emails and phones
    dup_start=perf_counter()            #track the dup time
    rep_key = rep.key(keys[:-1])        #create rep key without man/dup flag
    if 'NULL' in rep_key:               #dont use key with missing parts
        logging.debug("bad rep key")
        return 0

    search_party =  sfcontact.objects.none()        #contains the contacts that have similar fields to the rep

    sf_pd = pd.read_hdf('sf_contacts.hdf')


    #refactor using Q() object
    for key in keys[:-1]:
        if 'Phone' in key:              #for phones, check the variation of phones in the sales force contact
            for type_of_phone in ['mobilePhone', 'homePhone', 'otherPhone', 'Phone']:
                # create filter for each phone variation
                kwargs = {f'{type_of_phone}__icontains': f'{rep.key([key])}'}
                # queryset of Sfcontacts that have a matching field with the rep
                search_party = search_party.union(sfcontact.objects.filter(**kwargs))
        if 'Email' in key:
            for type_of_email in ['workEmail', 'personalEmail', 'otherEmail']:
                kwargs = {f'{type_of_email}__icontains': f'{rep.key([key])}'}
                search_party = search_party.union(sfcontact.objects.filter(**kwargs))

        kwargs = { f'{key}__icontains' : f'{rep.key([key])}' }
        # queryset of Sfcontacts that have a matching field with the rep
        search_party = search_party.union(sfcontact.objects.filter(**kwargs))

    #create list of keys mapped to the contact
    sf_map = {}
    for n, i in enumerate(keys):
        if 'Phone' in i:            #create a phone variations of each key
            multi_key = True
            for j in ['mobilePhone', 'homePhone', 'otherPhone', 'Phone']:
                vary_key = keys.copy()
                vary_key[n] = j
                addon = {i.key(vary_key[:-1]) : i for i in search_party if "NULL" not in i.key(vary_key[:-1])}
                sf_map = {**sf_map, **addon} #update sf_map with variation keys
        elif 'Email' in i:          #create an email variations of each key
            multi_key = True
            for j in ['workEmail', 'personalEmail', 'otherEmail']:
                vary_key = keys.copy()
                vary_key[n] = j
                addon = {i.key(vary_key[:-1]): i for i in search_party if "NULL" not in i.key(vary_key[:-1])}
                sf_map = {**sf_map, **addon}        #update sf_map with variation keys
    if not multi_key:
        sf_map = {i.key(keys[:-1]): i for i in search_party if "NULL" not in i.key(keys[:-1])}  # only returns

    #list of keys for fuzzy mapping
    sf_keys = sf_map.keys()

    #get closest
    closest = fuzzyset_alg(rep_key, sf_keys)
    if len(closest) == 0:
         logging.debug("no close matches")
         if currKey == keylist[-1]:
            string_key = '-'.join(currKey)
            rep.keySortedBy = string_key
            rep.type = sort(1)
            rep.save()
            return
         else:
            return
    for i in closest:
        #replace key with sf contact record
        i[0] = sf_map[i[0]]
    if len(closest) == 3  and closest[0][1] <= closest[-1][1] + 10 :        #see if 3rd closest is within 10% variation
        rep.average = np.mean([closest[0][1], closest[1][1], closest[2][1]]) #compute average
        #store contacts
        rep.closest1 = closest[0][0]
        rep.closest2 = closest[1][0]
        rep.closest3 = closest[2][0]
        rep.closest1_contactID = closest[0][0].ContactID
        rep.closest2_contactID = closest[1][0].ContactID
        rep.closest3_contactID = closest[2][0].ContactID
    elif  len(closest) == 2 and closest[0][1] <= closest[-1][1] + 5: #see if 2nd  closest is within 5% variation
        rep.average = np.mean([closest[0][1], closest[1][1]])
        rep.closest1 = closest[0][0]
        rep.closest2 = closest[1][0]
        rep.closest1_contactID = closest[0][0].ContactID
        rep.closest2_contactID = closest[1][0].ContactID
    else:
        rep.average = closest[0][1]
        rep.closest1 = closest[0][0]
        rep.closest1_contactID = closest[0][0].ContactID
    rep.type = sort(rep.average)

    if rep.type=='Duplicate' and rep.CRD != '' and  closest[0][0].CRD != '' and  int(rep.CRD.replace(".0","")) != int(closest[0][0].CRD.replace(".0","")) :
        rep.type = 'Manual Check'
    string_key = '-'.join(currKey)
    rep.keySortedBy = string_key
    rep.save()
    # logging.debug(f'{rep.firstName} sorted as {rep.type} with {rep.keySortedBy} key ')
    time = round(perf_counter()-dup_start, 2)

    #store time data
    dups = len(repContact.objects.filter(type='Duplicate'))
    news = len(repContact.objects.filter(type='New Record'))
    undies = len(repContact.objects.filter(type='Undecided'))
    avg = dedupTime.objects.aggregate(Avg('seconds'))['seconds__avg']
    if avg == None:
        avg = 0
    else:
        avg = round(avg, 2)
    dedupTime.objects.create(
                             seconds=time,
                             num_threads=numthreads,
                             avg=avg,
                             num_dup=dups,
                             num_new=news,
                             num_undie=undies,
                             current_key=currKey)
    cnt += 1

    #garbage collection
    del time, avg, dups, news, undies, string_key, sf_map, sf_keys, search_party, dup_start, rep_key

#reset flags and storage time
def finish(numThreads):
    global waiting, complete, times, start, reps_df
#     c = collect()  #garbage collection
    complete = True
    end = perf_counter()
    time = end - start
    # os.system('say "The repp list has been duplified!"')
    print(f'avg dup time: {round(np.average(times),2)}')
    print(f'total dedupe time: {datetime.timedelta(seconds=round(time))}')
    waiting=False
    dedupper.threads.stop_updates()
    reps_df.update(pd.Series(reps_ID_update_list, name='Id'))#fuzzy match the key against the key list and returns the 3 closest from the key list
    save_dfs()

def fuzzyset_alg(key, key_list):
    finder = FuzzySet()
    finder.add(key)
    candidates = list()
    for i in key_list:
        try:
            added = [i]
            #if the match score is below 50% key error raises
            matched = finder[i]
            added.extend(*matched)
            del added[-1]       #remove rep's key from list
            added[1] *= 100      #convert to percentage
            '''
            [0] the sf key
            [1] match percentage
            '''
            candidates.append(added)
        except:
            pass
    #sort by score
    candidates.sort(key=lambda x: x[1], reverse=True)

    #take top take 10
    top_candi = candidates[:10]
    #fuzzy match and sort again
    finalist = [[i[0], fuzz.ratio(key, i[0])] for i in top_candi]
    finalist.sort(key=lambda x: x[1], reverse=True)
    del finder, candidates, top_candi
    if len(finalist) > 0:
        return finalist[:3]
    else:
        return []

def key_dedupe(keys):
    global rep_key_map, reps_df
    print(f'keys: {keys}')
    # extract reps
    reps = reps_df

    # get reps without a matching sf record
    unmatched_reps = reps[reps.Id.isnull()]

    # if 'CRD__c' in keys and len(keys) == 1:
    # rep_key_map = { crd: i for }

    # map of the rep keys => index in dataframe
    #np.add.reduce => concatenates the provided columns for each rep
    rep_key_map = {rep_key: i for i, rep_key in
                   enumerate(list(np.add.reduce(reps[keys].astype(str).fillna('NULL'), axis=1))
                             ) if i in unmatched_reps.index and 'NULL' not in rep_key}

    print('adding {} items to the Q'.format(len(rep_key_map)))
    # add them all to thread Q
    dedupper.threads.dedupeQ([[rep_key, keys] for rep_key in rep_key_map.keys()])
    del unmatched_reps, reps
    collect()

def threaded_deduping(index, ln):
    global reps_df, sf_df, rep_key_map, times, sf_groups, reps_ID_update_list, manual_dict
    start = perf_counter()
    data = defaultdict(int)

    # THREADED PART
    for key in keylist:
        sf_keys = [sf_df.iloc[i][key] for i in sf_groups.groups[ln]]
        possibilities = fuzzyset_alg(reps_df.iloc[index][key], sf_keys)
        manuals = [possible[0] for i, possible in enumerate(possibilities) if 95 <= possible[1] < 97]
        non_match = [possible[0] for i, possible in enumerate(possibilities) if possible[1] < 95]
        if possibilities and possibilities[0][1] >= 97:
            reps_ID_update_list[index] = sf_df[sf_df[key] == possibilities[0][0]].iloc[0]['Id']
            break
        elif manuals:
            manual_dict[index] = [i for i in sf_groups.groups[ln] if sf_df.iloc[i][key] in manuals]
            reps_ID_update_list[index] = 'manual'
            print('manual check')
            break
    time = abs(perf_counter() - start)
    times.append(time)
    if (len(times) % 100 == 0):
        print(f'averaging {np.average(times)} secs')

# report number found with CRD
# given estimate with number of reps, times average

#     del sf_key_map, sf_contacts
#the start of duplify algorithm
def key_generator(data):
    print('ENTER: key_generator')
    print('	 ENTER: import_contacts')
    import_contacts(data['reps'], data['map'], data['channel'])
    print('	 EXIT: import_contacts')
#     import_gatekeepers()
    global sf_df, reps_df, start, waiting, doneKeys, totalKeys, cnt, currKey, sort_alg, keylist, sf_groups, reps_ID_update_list
    matched= False
    manual_dict=dict()
    print('start your engines ')
    start = perf_counter()

    print('	 ENTER: preprocess')
    sf_df, reps_df, keylist = preprocess(sf_df, reps_df, data['keys'])
    print('	 EXIT: preprocess')

    print('	 ENTER: match_crds')
    reps_ID_update_list = match_crds(sf_df, reps_df) #start timer
    print('	 EXIT: match_crds')
#     reps_df, sf_df = get_contacts('both')

#     sf_groups = {i: sf_df.groupby(i).groups for i in list(sf_df.columns)[:-1]}
    reps_groups = reps_df.groupby('LastName')
    sf_groups = sf_df.groupby('LastName')#index using groups by lastname
    reps_lastnames = list(set(reps_groups.groups.keys()))
    sf_lastnames = list(set(sf_groups.groups.keys()))
    last_name_groups = list(set(reps_lastnames).intersection(sf_lastnames))

    print('	 ENTER: key_deduper')
    key_deduper(last_name_groups, reps_groups, reps_ID_update_list)
    print('	 EXIT: key_deduper')
    print('ENTER: finish')
    finish(30)
    print('EXIT: finish')
    print('EXIT: key_generator')

def key_deduper(last_name_groups, reps_groups, reps_ID_update_list):
    matched= False
    manual_dict=dict()
    start = perf_counter()

    rep_indices = [[(index, ln) for index in  reps_groups.groups[ln] if reps_ID_update_list[index] == ''] for ln in last_name_groups]
    rep_list = reduce(lambda x, y: list(chain(x,y)), rep_indices)
    print('	 ENTER: threads.dedupeQ')
    dedupper.threads.dedupeQ(rep_list)
    print('	 EXIT: threads.dedupeQ')
#uploades contacts to the db
def load_csv2db(csv, header_map, resource, file_type='rep'):
    global done
    start = perf_counter()
    dataset = Dataset()
    pd_csv = csv
    csv_header = list(pd_csv)

    try:
        if file_type=='rep':
            #concatentate the records data into a misc fields for later restoration
            pd_csv['misc'] = misc_col(csv, csv_header)
        #map replist headers to db headers
        pd_csv.rename(columns=header_map, inplace=True)
        #add id col for django import export
        pd_csv['Id'] = np.nan

        #import contact records
        dataset.csv = pd_csv.to_csv()
        resource.import_data(dataset, dry_run=False)
    except:
        print("lost the pandas csv")
    end = perf_counter()    #stop timer
    time = end - start
    if file_type == 'rep':
        done = True
        uploadTime.objects.create(num_records = len(repContact.objects.all()), seconds=round(time, 2))
    else:
        uploadTime.objects.create(num_records = len(sfcontact.objects.all()),seconds=round(time, 2))
    return csv_header

# concatenates cols of the df
def misc_col(df, cols):
    return reduce(lambda x, y: x.astype(str).str.cat(y.astype(str), sep='-!-'), [df[col] for col in cols])

#generates stats for each fields based on uniqueness of values and amount of blanks
def make_keys():
    global key_stats
    keys = []
    excluded = ['id', 'average', 'type', 'match_ID', 'closest1', 'closest2', 'closest3',
                'closest1_contactID', 'closest2_contactID', 'closest3_contactID', 'dupFlag', 'keySortedBy', 'misc']


    reps = pd.read_json(RepContactResource().export().json)
    SFs = pd.read_json(SFContactResource().export().json)
    [df.replace('', np.nan, inplace=True) for df in [reps, SFs]]

    sf_count = 100*(SFs.count() / float(len(SFs)) )
    rep_count = 100*(reps.count() / float(len(reps)) )


    for i in set(reps.columns).intersection(set(SFs.columns)):
        if i not in excluded:
            if reps[i].count() != 0:
                rp_utility = int(rep_count[i])
                sf_utility = int(sf_count[i])
                if reps[i].count() > 0 :
                    rp_uniqueness = int(float(float(len(reps[i].unique()) )/ float(reps[i].count())) *100)
                else:
                    rp_uniqueness = 0
                if SFs[i].count() > 0:
                    sf_uniqueness = int(float(float(len(SFs[i].unique())) / float(SFs[i].count()))*  100)
                else:
                    sf_uniqueness = 0

                score =   np.average([ rp_uniqueness, rp_utility, sf_uniqueness, sf_utility])

                stat = (i, rp_uniqueness, rp_utility, sf_uniqueness, sf_utility, score)
            else: stat = (i,0,0,0,0,0)
            keys.append(stat)
    keys.sort(key=itemgetter(5), reverse=True)
    return keys

def match_keys(key,key_list):
    for i in key_list:
        yield match_percentage(key, i)

def match_percentage(key1,key2):
    return fuzz.ratio(key1, key2)

def mutate(keys):
    mutant = keys.copy()
    num_mutating = randint(int(len(keys)/5),int(len(keys)*0.8))

    for i in range(num_mutating):
        j = randint(0,len(keys)-1)
        for i in range(randint(3,len(mutant[j])+3)):
            mutant[j]=mutant[j].replace(mutant[j][int(sample(range(len(mutant[j])-1), 1)[0])], choice(string.printable))
    return mutant

#change the sort algorithms and resort all records
def set_sorting_algorithm(min_dup, min_uns):
    global standard_sorting_range, manual_sorting_range
    cnt=0
    standard_sorting_range = RangeKeyDict({
    (min_dup, 101): 'Manual Check',
    (0, min_dup): 'Undecided',
})

    manual_sorting_range = RangeKeyDict({
    (min_dup, 101): 'Manual Check',
    (min_uns, min_dup): 'Undecided',
    (0, min_uns): 'New Record'
})
    for rep in list(repContact.objects.all()):
        cnt+=1
        if rep.keySortedBy != '':
            keys = rep.keySortedBy.split('-')
            if keys == keylist[-1]:
                rep.type = last_key_sorting_range[rep.average]
            elif keys[-1] == 'true':
                rep.type = manual_sorting_range[rep.average]
            else:
                rep.type = standard_sorting_range[rep.average]
            rep.save()
        else:
            rep.type = standard_sorting_range[rep.average]
            rep.save()
            print('{}-{}'.format(rep.type, rep.average))

        if cnt%500 ==0:
            print('re-sort #{}'.format(cnt))

#determines which sorting algorithm will be used for the key
def sort(avg):
    if sort_alg == 'true' and currKey == keylist[-1]:
        return last_manual_sorting_range[avg]
    elif sort_alg == 'true':
        return manual_sorting_range[avg]
    elif currKey == keylist[-1]:
        return last_key_sorting_range[avg]
    else:
        return standard_sorting_range[avg]
#returns data for the progress screeen
def get_progress():
    return doneKeys, totalKeys, currKey, cnt

def get_channel(data):
    begin = perf_counter()
    global done
    channel = data['channel']
    rep_header_map = data['map']
    rep_df = data['reps']
    print(f'old columns = {list(rep_df.columns)}')
    rep_df.rename(columns=rep_header_map,
                  inplace=True)
    print(f'new columns = {list(rep_df.columns)}')

    print('loading sf: STARTED')
    sf = Salesforce(password='7924trill', username='jmadubuko@wealthvest.com',
                    security_token='W4ItPbGFZHssUcJBCZlw2t9p2')
    query = "select Id, CRD__c, FirstName, LastName, Suffix, MailingStreet, MailingCity, MailingState, MailingPostalCode, Phone, MobilePhone, HomePhone, otherPhone, Email, Other_Email__c, Personal_Email__c   from Contact where Territory_Type__c='Geography' and Territory__r.Name like "
    starts_with = f"'{channel}%'"
    print ('querying SF')
    territory = sf.bulk.Contact.query(query + starts_with)
    print(len(territory))
    territory = pd.DataFrame(territory).drop('attributes', axis=1).replace([None], ['NULL'], regex=True)
    #store hdf
    # territory.to_hdf('sf_contact.hdf', 'sf', mode='w')



    sf_header_map = {
        'CRD__c': 'CRD',
        'Email': 'workEmail',
        'FirstName': 'firstName',
        'HomePhone': 'homePhone',
        'Id': 'ContactID',
        'LastName': 'lastName',
        'MailingCity': 'mailingCity',
        'MailingPostalCode': 'mailingZipPostalCode',
        'MailingState': 'mailingStateProvince',
        'MailingStreet': 'mailingStree t',
        'MobilePhone': 'mobilePhone',
        'OtherPhone': 'otherPhone',
        'Phone': 'Phone',
        'Personal_Email__c': 'personalEmail',
        'Other_Email__c': 'otherEmail',
        'Suffix': 'suffix',
    }

    # sfcontact_resource = SFContactResource()
    # repcontact_resource = RepContactResource()
    # load_csv2db(territory, sf_header_map,
    # sfcontact_resource, file_type='SF')
    # territory.rename(columns=sf_header_map, inplace=True)

    print('loading sf: DONE')

    territory['unmatched'], rep_df["Id"] = True, np.nan
    print('loading rep: STARTED')
    # load_csv2db(rep_df, rep_header_map, repcontact_resource)
    print('loading rep: DONE')
    # print('key stats: STARTED')
    # # make_keys()
    # print('key stats: DONE')
    print('job: DONE')
    data = rep_df.to_csv() + '--$--' + territory.to_csv()
    pr = progress(label=data, total_reps=len(rep_df))
    pr.save()
    db.connections.close_all()
    print(f'size of data sent: {len(data)} ')
    del data, territory, rep_df
    c = collect()                   #garbage collection
    logging.debug(f'# of garbage collected after importing records = {c}')
    fin = perf_counter()
    print(f'####contacts stored in {round(fin-begin)} secs')

    return True

def get_key_stats():
    return key_stats

def completed():
    return complete

def db_done():
    try:
        d = pd.read_pickle(settings.SF_CSV)
        print (type(d))
        status =list(d['status'] == 1)[0]
        print (f'status: {status}')
        if status:
             return status
    except Exception as e:
        print(e)
        return

def get_contacts(c_filter):
    reps, sf = progress.objects.latest().label.split('--$--')

    contact_getter = {
        'sf': pd.read_csv(StringIO(sf), dtype=str, index_col=0, true_values=['True'], false_values=['False'], ),
        'reps': pd.read_csv(StringIO(reps), dtype=str, index_col=0, true_values=['True'], false_values=['False'], ),
        'both': (pd.read_csv(StringIO(reps), dtype=str, index_col=0, true_values=['True'], false_values=['False'], ),
                 pd.read_csv(StringIO(sf), dtype=str,
                             index_col=0, true_values=['True'], false_values=['False'], ))
    }
    db.connections.close_all()
    collect()

    return contact_getter[c_filter]

def update_df(update):
    global reps_df, sf_df
    reps_df.loc[update[0], 'Id'] = sf_df.loc[update[1], 'Id']
    sf_df.loc[update[1], 'unmatched'] = False
    p = progress.objects.latest()
    p.update(completed_reps=progress.objects.latest().completed_reps + 1)
    p.save()
    db.connections.close_all()

def save_dfs():
    global reps_df, sf_df
    print('saving dataframes')
    unmatched_reps = reps_df[reps_df.Id == 'manual']
    print(f'reps matched: {len(unmatched_reps)}')
    data = reps_df.to_csv() + '--$--' + sf_df.to_csv()
    p = progress.objects.latest()
    p.label = data
    p.completed_keys = progress.objects.latest().completed_keys + 1
    p.save()
    db.connections.close_all()
    del data, unmatched_reps
    collect()

def import_contacts(rep_file, df_map, channel):
    # check rep_file type
    global reps_df, sf_df, loaded, reps_ID_update_list, sf_match_update_list, reps_type_update_list, reps_avg_update_list
    if not loaded:
        loaded=True
        reps_df = rep_file
        reps_df.rename(columns=df_map, inplace=True)
        sf = Salesforce(password='7924trill', username='jmadubuko@wealthvest.com', security_token='W4ItPbGFZHssUcJBCZlw2t9p2')
        query = "select Id, CRD__c, FirstName, LastName, Suffix, MailingStreet, MailingCity, MailingState, MailingPostalCode, Phone, MobilePhone, HomePhone, otherPhone, Email, Other_Email__c, Personal_Email__c   from Contact where Territory_Type__c='Geography' and Territory__r.Name like "
        starts_with = f"'{channel}%'"
        territory = sf.bulk.Contact.query(query + starts_with)
        sf_df = pd.DataFrame(territory, dtype=str).drop('attributes', axis=1).replace([None], ['NULL'], regex=True)
        sf_df['unmatched'], reps_df["Id"], reps_df["Type"], reps_df["avg"]  = True, np.nan, np.nan, np.nan
        reps_ID_update_list = [np.nan for _ in range(len(reps_df))]
        reps_type_update_list = [np.nan for _ in range(len(reps_df))]
        reps_avg_update_list = [np.nan for _ in range(len(reps_df))]
        sf_match_update_list  = [True for _ in range(len(sf_df))]
        data = reps_df.to_csv() + '--$--' + sf_df.to_csv()
        print(f'before {progress.objects.count()}')
        pr = progress(label=data, total_reps=len(reps_df))
        pr.save()
        print(f'after {progress.objects.count()}')
        print(f'after {pr.completed_keys}')

        db.connections.close_all()

def preprocess(sfdf, repdf, keys):
    key_list = list()

    '''preprocessing'''
    sfdf.update(clean(sfdf.FirstName))
    sfdf.update(clean(sfdf.LastName))
    sfdf.update(clean(sfdf.Email))
    sfdf.update(clean(sfdf.MailingState))
    sfdf.update(phonenumbers(sfdf.MailingPostalCode))
    sfdf.update(clean(sfdf.MailingCity))
    sfdf.update(phonenumbers(sfdf.Phone))
    sfdf.update(clean(sfdf.CRD__c.astype(str)))

    repdf.update(clean(repdf.FirstName))
    repdf.update(clean(repdf.LastName))
    repdf.update(clean(repdf.Email))
    repdf.update(clean(repdf.MailingState))
    repdf.update(phonenumbers(repdf.MailingPostalCode))
    repdf.update(clean(repdf.MailingCity))
    repdf.update(phonenumbers(repdf.Phone))
    repdf.update(clean(repdf.CRD__c.astype(str)))

    '''key generating'''
    for df in [sfdf, repdf]:
        for key in keys:
            if len(key[:-1]) > 1:
                key_col = ''.join([''.join(c for c in s if c.isupper()) for s in key[:-1]])
                if key_col not in key_list:
                    key_list.append(key_col)
                df[key_col] = pd.Series(np.add.reduce(df[key[:-1]].astype(str), axis=1))
            else:
                if key[0] not in key_list:
                    key_list.append(key[0])
    return sfdf, repdf, key_list

def match_crds(sfdf, repdf):
    lt_ID_update_list = list()
    sfcrd = set(sfdf.CRD__c)
    repcrd = list(repdf.CRD__c)
#     print(len(sfcrd), len(repcrd))

    #find intersection of CRD
    CRD_matches = set(sfcrd).intersection(set(repcrd))
    lt_ID_update_list = ['' for _ in repcrd]

    #match those records
    for crd in CRD_matches:
        lt_ID_update_list[repdf[repdf.CRD__c == crd].iloc[0].name] = sfdf[sfdf.CRD__c==crd].iloc[0]["Id"]
    return lt_ID_update_list
