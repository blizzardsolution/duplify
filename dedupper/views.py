from django.shortcuts import render
from django.views.generic import TemplateView, ListView
import django_tables2
from dedupper.models import dedupTime, duplifyTime, uploadTime,  sfcontact, repContact, progress
from  dedupper.filters import SimpleFilter
from dedupper.tables import SimpleTable, ContactTable, RepContactTable
from tablib import Dataset
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render, redirect
from dedupper.forms import UploadFileForm
from django_filters.views import FilterView
from django_tables2.views import SingleTableMixin, RequestConfig

from dedupper.resources import RepContactResource, SFContactResource, DedupTimeResource, DuplifyTimeResource, UploadTimeResource
from  dedupper.utils import key_generator, makeKeys, convertCSV, getProgress
import csv
from collections import OrderedDict
'''
#TODO change the HTTP request 
timeout for the running algorithm or 
have a middle man page that loads and send an request for 
TODO set up postgresql
#TODO https://medium.com/@johngrant/django-and-heroku-postgres-databases-6c22ffd71081 
https://medium.com/agatha-codes/9-straightforward-steps-for-deploying-your-django-app-with-heroku-82b952652fb4
http://www.marinamele.com/2013/12/how-to-set-django-app-on-heroku-part-i.html
https://www.youtube.com/watch?v=P8_wDttTeuk
'''
keys= []

#TODO look into make custom commands reset the contacts type and closest matches
#TODO look make custom command to spit out number of each model
    #https://docs.djangoproject.com/en/2.0/howto/custom-management-commands/
#TODO find how to for django celery and bootstrap progress bar
    #https://www.dangtrinh.com/2013/07/django-celery-display-progress-bar-of.html
    #https://github.com/czue/celery-progress
    #https://stackoverflow.com/questions/7380373/django-celery-progress-bar


def index(request):
    return render(request, 'dedupper/rep_list_upload.html')

def run(request):
    if request.method == 'GET':

        keylist = request.GET.get('keys')
        # read in channel and query SF by channgel for the key gen
        # channel = request.POST.get('channel')
        print('Starting algorithm with {}'.format(keylist))
        keylist = keylist.split("_")
        partslist = [i.split('-') for i in keylist[:-1]]
        result = key_generator(partslist)
        # result = key_generator.delay(partslist)
    # return render(request, "dedupper/loading_page.html", context={'task_id': result.task_id})
    return JsonResponse({'msg': 'success!'}, safe=False)

def progress(request):
    if request.method == 'GET':
        reps = len(repContact.objects.all())
        dups = len(repContact.objects.filter(type='Duplicate'))
        news = len(repContact.objects.filter(type='New Record'))
        undies = len(repContact.objects.filter(type='Undecided'))
        doneKeys, numKeys, currKey, doneReps = getProgress()
    return JsonResponse({'reps': reps, 'dups':dups, 'news': news, 'undies':undies, 'doneKeys': doneKeys,
                         'numKeys':numKeys, 'doneReps':doneReps, 'currKey':currKey}, safe=False)

#connect this page with filters config = RequestConfig(request)
def display(request):

    config = RequestConfig(request, paginate={'per_page': 1000})
    undecided_table = RepContactTable(repContact.objects.filter(type__exact='Undecided'), prefix='U-')  # prefix specified
    duplicate_table = RepContactTable(repContact.objects.filter(type__exact='Duplicate'), prefix='D-')  # prefix specified
    new_record_table = RepContactTable(repContact.objects.filter(type__exact='New Record'), prefix='N-')  # prefix
    # specified
    config.configure(undecided_table)
    config.configure(duplicate_table)
    config.configure(new_record_table)

    undecided = RepContactResource().export(repContact.objects.filter(type='Undecided'))
    newRecord = RepContactResource().export(repContact.objects.filter(type='New Record'))
    duplicate = RepContactResource().export(repContact.objects.filter(type='Duplicate'))

    return render(request, 'dedupper/sorted.html', {
        'undecided_table': undecided_table,
        'duplicate_table': duplicate_table,
        'new_record_table': new_record_table,
    })

def upload(request):
    global export_headers, keys
    repcontact_resource = RepContactResource()
    sfcontact_resource = SFContactResource()
    dataset = Dataset()

    print('uploading file')
    form = UploadFileForm(request.POST, request.FILES)
    repCSV = request.FILES['repFile']
    sfCSV = request.FILES['sfFile']
    headers = convertCSV(repCSV, repcontact_resource, batchSize=100)
    export_headers = headers
    convertCSV(sfCSV, sfcontact_resource, type='SF', batchSize=100)

    keys = makeKeys(headers)

    return redirect('/key-gen/', {'keys': keys})

def merge(request, CRD):
    obj = repContact.objects.values().get(CRD=CRD)
    ids = [obj['closest1_contactID'], obj['closest2_contactID'], obj['closest3_contactID']]
    objs = sfcontact.objects.values().filter(ContactID__in=ids)
    fields = [i.name for i in repContact._meta.local_fields]
    mergers = list()

    for i in range(len(objs)):
        if objs[i]['ContactID'] == obj['closest1_contactID']:
            del objs[i]['closest_rep_id'], objs[i]['dupFlag'], objs[i]['ContactID']
            mergers.insert(0, list(objs[i].values()))
        elif objs[i]['ContactID'] == obj['closest2_contactID']:
            del objs[i]['closest_rep_id'], objs[i]['dupFlag'], objs[i]['ContactID']
            mergers.insert(len(mergers), list(objs[i].values()))
        else:
            del objs[i]['closest_rep_id'], objs[i]['dupFlag'], objs[i]['ContactID']
            mergers.insert(-1, list(objs[i].values()))

    del obj['closest1_id'], obj['closest2_id'], obj['closest3_id'], obj['closest1_contactID'], \
        obj['closest2_contactID'], obj['closest3_contactID'], obj['type'], obj['dupFlag'], obj['average']
    obj = list(obj.values())
    if len(mergers) == 3:
        obj_map = {i:j for i,j in zip(fields, list(zip(obj,mergers[0],mergers[1],mergers[2])) ) }
    elif len(mergers) == 2:
        obj_map = {i:j for i,j in zip(fields, list(zip(obj,mergers[0],mergers[1])) ) }
    elif len(mergers) == 1:
        obj_map = {i:j for i,j in zip(fields, list(zip(obj,mergers[0])) ) }
    return render(request, 'dedupper/merge.html', {'objs' : obj_map})

def download(request,type):
    export_headers = list(list(repContact.objects.all().values())[0].keys())

    if(type == "Duplicate"):
        filename = 'filename="Duplicates.csv"'
    elif(type == "NewRecord"):
        filename = 'filename="New Records.csv"'
        type = 'New Record'
    else:
        filename = 'filename="Undecided Records.csv"'

    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; '+filename

    writer = csv.writer(response)
    writer.writerow(export_headers)

    users = repContact.objects.filter(type = type).values_list(*export_headers)
    for user in users:
        writer.writerow(user)

    return response

def download_times(request,type):
    if(type == "DD"):
        filename = 'filename="Dedup Times.csv"'
        times = dedupTime.objects.all().values_list()
        export_headers = [i.name for i in dedupTime._meta.local_concrete_fields]
    elif(type == "D"):
        filename = 'filename="Duplify Times.csv"'
        times = duplifyTime.objects.all().values_list()
        export_headers = [i.name for i in duplifyTime._meta.local_concrete_fields]
    else:
        filename = 'filename="Upload Times.csv"'
        times = uploadTime.objects.all().values_list()
        export_headers = [i.name for i in uploadTime._meta.local_concrete_fields]


    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; '+filename
    writer = csv.writer(response)
    writer.writerow(export_headers)

    for time in times:
        writer.writerow(time)

    return response

def key_gen(request):
    key = makeKeys(list(list(repContact.objects.all().values())[0].keys()))
    return render(request, 'dedupper/key_generator.html', {'keys': key})

def loading(request,keylist):
    return render(request, 'dedupper/loading_page.html', {'keylist':keylist})

def duplify(request):
    if request.method == 'GET':
        keylist = request.GET.get('keylist')
        print('Starting algorithm with {}'.format(keylist))
        keylist = keylist.split("_")
        partslist = [i.split('-') for i in keylist[:-1]]
        result = key_generator.delay(partslist)

    return JsonResponse({'task_id': result.task_id}, safe=False)

