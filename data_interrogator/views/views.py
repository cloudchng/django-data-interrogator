from django.conf import settings
from django.contrib.auth.decorators import user_passes_test
from django.core import exceptions
from django.core.urlresolvers import reverse
from django.db.models import F, Count, Min, Max, Sum, Value, Avg, ExpressionWrapper, DurationField, FloatField, CharField
from django.db.models import functions as func
from django.http import JsonResponse, QueryDict
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import get_template
from django.template import Context
from django.views.generic import View

from django.contrib.auth.decorators import login_required
from django.utils.decorators import method_decorator

import tempfile, os

from datetime import timedelta

from data_interrogator.forms import AdminInvestigationForm, InvestigationForm
from data_interrogator.db import GroupConcat, DateDiff, ForceDate

# Because of the risk of data leakage from User, Revision and Version tables,
# If a django user hasn't explicitly set up a witness protecion program,
# we will ban interrogators from inspecting the User table
# as well as Revsion and Version (which provide audit tracking and are available in django-revision)
dossier = getattr(settings, 'DATA_INTERROGATION_DOSSIER', {})
witness_protection = dossier.get('witness_protection',["User","Revision","Version"])

def custom_table(request):
    return interrogation_room(request, 'data_interrogator/custom.html')


def get_suspect(app_label,model):
    from django.contrib.contenttypes.models import ContentType
    
    return ContentType.objects.get(app_label=app_label.lower(),model=model.lower()).model_class()
    

def column_generator(request):
    model = request.GET.get('model','')
    
    if model:
        app_label,model = model.split(':',1)
        lead_suspect = get_suspect(app_label,model)
        
        fields = [str(f.name) for f in lead_suspect._meta.fields]
        related_models = [f for f in lead_suspect._meta.get_all_field_names() if f not in fields]
        
    return JsonResponse({'model': model,'fields':fields,'related_models':related_models})

def interrogation_room(request,template='data_interrogator/custom.html'):
    return InterrogationRoom.as_view(template_name=template)(request)
    
class InterrogationRoom(View):
    form_class = InvestigationForm
    template_name = 'data_interrogator/interrogation_room.html'

    def get(self, request):
        data = {}
        form = self.form_class()
        has_valid_columns = any([True for c in request.GET.getlist('columns',[]) if c != ''])
        if request.method == 'GET' and has_valid_columns:
            # create a form instance and populate it with data from the request:
            form = self.form_class(request.GET)
            # check whether it's valid:
            if form.is_valid():
                # process the data in form.cleaned_data as required
                filters = form.cleaned_data.get('filter_by',[])
                order_by = form.cleaned_data.get('sort_by',[])
                columns = form.cleaned_data.get('columns',[])
                suspect = form.cleaned_data['lead_suspect']
                if request.user.is_staff and request.GET.get('action','') == 'makestatic':
                    # populate the appropriate GET variables and redirect to the admin site
                    base = reverse("admin:data_interrogator_datatable_add")
                    vals = QueryDict('', mutable=True)
                    vals.setlist('columns',columns)
                    vals.setlist('filters',filters)
                    vals.setlist('orders',order_by)
                    vals['base_model'] = suspect
                    return redirect('%s?%s'%(base,vals.urlencode()))
                else:
                    data = interrogate(suspect,columns=columns,filters=filters,order_by=order_by)
        data['form']=form
        return render(request, self.template_name, data)


class AdminInterrogationRoom(InterrogationRoom):
    template_name = 'data_interrogator/admin/analytics.html'
    form_class = AdminInvestigationForm

    @method_decorator(user_passes_test(lambda u: u.is_staff))
    def get(self, request):
        return super(AdminInterrogationRoom,self).get(request)

def datatable(request,url):
    table = get_object_or_404(data_interrogator.models.DataTablePage, url=url)

    filters = [f.filter_definition for f in table.filters.all()]
    columns = [c.column_definition for c in table.columns.all()]
    orderby = [f.ordering for f in table.order.all()]
    suspect = table.base_model
    
    template = "data_interrogator/by_the_book.html"
    if table.template_name:
        template = table.template_name

    data = interrogate(suspect,columns=columns,filters=filters,order_by=orderby,limit=table.limit)
    data['table'] = table
    return render(request, template, data)


def interrogate(suspect,columns=[],filters=[],order_by=[],headers=[],limit=None):
    errors = []
    suspect_data = {}
    annotation_filters = {}
    query_columns = []
    output_columns = []
    count=0
    app_label,model = suspect.split(':',1)
    lead_suspect = get_suspect(app_label,model)

    for suspect in dossier.get('suspects',[]):
        if suspect['model'] == (app_label,model):
            suspect_data = suspect
            
    annotations = {}
    wrap_sheets = suspect_data.get('wrap_sheets',{})
    aliases = suspect_data.get('aliases',{})
    available_annotations = {
            "min":Min,
            "max":Max,
            "sum":Sum,
            'avg':Avg,
            "count":Count,
            "substr":func.Substr,
            "concat":GroupConcat,
            "join":func.Concat,
        }
    expression_columns = []
    for column in columns:
        #column = normalise_field(column).lower()


        if column == "":
            continue # do nothings for empty fields
        if any("__%s__"%witness in column for witness in witness_protection):
            continue # do nothing for protected models
        var_name = None
        if '=' in column:
            var_name,column = column.split('=',1)
        elif column in aliases.keys():
            var_name = column
            column = aliases[column]['column']
        # map names in UI to django functions
        column = normalise_field(column).lower()
        if var_name is None:
            var_name = column
        if column.startswith(tuple([a+'___' for a in available_annotations.keys()])) and  " - " in column:
            # we're aggregating some mathy things, these are tricky
            split = column.split('___')
            aggs,field = split[0:-1],split[-1]
            agg = aggs[0]
            a,b = field.split(' - ')

            if a.endswith('date') and b.endswith('date'):
                expr = ExpressionWrapper(
                    DateDiff(
                        ForceDate(F(a)),
                        ForceDate(F(b))
                    ), output_field=DurationField()
                )
            else:
                expr = ExpressionWrapper(F(a)-F(b), output_field=CharField())
            
            annotations[var_name] = available_annotations[agg](expr, distinct=True)

            query_columns.append(var_name)
            output_columns.append(var_name)
            expression_columns.append(var_name)
            
        elif " - " in column:
            a,b = column.split(' - ')
            if a.endswith('date') and b.endswith('date'):
                annotations[var_name] = ExpressionWrapper(
                    DateDiff(
                        ForceDate(F(a)),
                        ForceDate(F(b))
                    ),
                    output_field=DurationField()
                )
            else:
                annotations[var_name] = ExpressionWrapper(F(a)-F(b), output_field=CharField())
            query_columns.append(var_name)
            output_columns.append(var_name)
            expression_columns.append(var_name)
        elif column.startswith(tuple([a+'___' for a in available_annotations.keys()])):
            agg,field = column.split('___',1)
            if agg == 'join':
                fields = []
                for f in field.split(','):
                    if f.startswith(('"',"'")):
                        # its a string!
                        fields.append(Value(f.strip('"').strip("'")))
                    else:
                        fields.append(f)
                annotations[var_name] = available_annotations[agg](*fields)
            elif agg == "substr":
                field,i,j = (field.split(',')+[None])[0:3]
                annotations[var_name] = available_annotations[agg](field,i,j)
            else:
                annotations[var_name] = available_annotations[agg](field, distinct=True)
            #if agg in available_annotations.keys():
                #annotation_filters[field]=F(column)
            query_columns.append(var_name)
            output_columns.append(var_name)
        else:
            if column in wrap_sheets.keys():
                cols = wrap_sheets.get(column).get('columns',[])
                query_columns = query_columns + cols
            else:
                query_columns.append(var_name)
            if var_name != column:
                annotations[var_name] = F(column)
            output_columns.append(var_name)

    rows = lead_suspect.objects

    _filters = {}
    excludes = {}
    filters_all = {}
    for i,expression in enumerate(filters + [v['filter'] for k,v in aliases.items() if k in columns]):
        cleaned = clean_filter(normalise_field(expression))
        if '|' in cleaned:
            field,exp = cleaned.split('|')
            key,val = (field+exp).split("=",1)
        else:
            key,val = cleaned.split("=",1)
            field = key
        key = key.strip()
        val = val.strip()
        
        if val.startswith('~'):
            val = F(val[1:])
        elif key.endswith('date'): # in key:
            val = (val+'-01-01')[:10] # If we are filtering by a date, make sure its 'date-like'
        elif key.endswith('__isnull'):
            if val == 'False' or val == '0':
                val = False
            else:
                val = bool(val)

        if '___' in field:
            # we got an annotated filter
            agg,f = field.split('___',1)
            field = 'f%s%s'%(i,field)
            key = 'f%s%s'%(i,key)
            annotations[field] = available_annotations[agg](f, distinct=True)
            if field not in query_columns:
                query_columns.append(field)
            annotation_filters[key] = val
        elif key in annotations.keys():
            annotation_filters[key] = val
        elif key.split('__')[0] in expression_columns:
            k = key.split('__')[0]
            if 'date' in k and key.endswith('date') or 'date' in str(annotations[k]):
                #1/0
                val,period = (val.rsplit(' ',1) + ['days'])[0:2] # this line is complicated, just in case there is no period or space
                period = period.rstrip('s') # remove plurals
                
                kwargs = {}
                big_multipliers = {
                    'day':1,
                    'week':7,
                    'fortnight': 14, # really?
                    'month':30, # close enough
                    'sam':297,
                    'year': 365,
                    'decade': 10*365, # wise guy huh?
                    }
                    
                little_multipliers = {
                    'second':1,
                    'minute':60,
                    'hour':60*60,
                    'microfortnight': 1.2, # sure why not?
                    }
                    
                if big_multipliers.get(period,None):
                    kwargs['days'] = int(val)*big_multipliers[period]
                elif little_multipliers.get(period,None):
                    kwargs['seconds'] = int(val)*little_multipliers[period]
                    
                annotation_filters[key] = timedelta(**kwargs)
                    
            else:
                annotation_filters[key] = val

        elif key.endswith('__all'):
            key = key.rstrip('_all')
            val = [v for v in val.split(',')]
            filters_all[key] = val
        else:
            exclude = key.endswith('!')
            if exclude:
                key = key[:-1]
            if key.endswith('__in'):
                val = [v for v in val.split(',')]
            if exclude:
                excludes[key] = val
            else:
                _filters[key] = val
    filters = _filters

    try:

        rows = rows.filter(**filters)
        for key,val in filters_all.items():
            for v in val:
                rows = rows.filter(**{key:v})
        rows = rows.exclude(**excludes)

        if annotations:
            rows = rows.annotate(**annotations)
            rows = rows.filter(**annotation_filters)
        if order_by:
            ordering = map(normalise_field,order_by)
            rows = rows.order_by(*ordering)

        if limit:
            lim = abs(int(limit))
            rows = rows[:lim]
        rows = rows.values(*query_columns)

        count = rows.count()
        rows[0] #force a database hit to check the state of things
    except ValueError,e:
        rows = []
        errors.append("Limit must be a number greater than zero")
    except IndexError,e:
        rows = []
        errors.append("No rows returned for your query, try broadening your search.")
    except exceptions.FieldError,e:
        rows = []
        if str(e).startswith('Cannot resolve keyword'):
            field = str(e).split("'")[1]
            errors.append("The requested field '%s' was not found in the database."%field)
        else:
            errors.append("An error was found with your query:\n%s"%e)
    except Exception,e:
        rows = []
        errors.append("Something when wrong - %s"%e)

    return {'rows':rows,'count':count,'columns':output_columns,'errors':errors, 'suspect':suspect_data,'headers':headers }

def normalise_field(text):
    return text.strip().replace('(','___').replace(')','').replace(".","__")

def clean_filter(text):
    maps = [('<=','lte'),('<','lt'),('<>','ne'),('>=','gte'),('>','gt')]
    for a,b in maps:
        # We add a bar cause we need to to a split to get the actual field outside of here.
        text = text.replace(a,'|__%s='%b)
    return text

def clean_sort_columns(text):
    return [normalise_field(v) for v in text.split(',')]




    
def datatable(request,url):
    table = get_object_or_404(data_interrogator.models.DataTablePage, url=url)

    filters = [f.filter_definition for f in table.filters.all()]
    columns = [c.column_definition for c in table.columns.all()]
    orderby = [f.ordering for f in table.order.all()]
    suspect = table.base_model
    
    template = "data_interrogator/by_the_book.html"
    if table.template_name:
        template = table.template_name

    data = interrogate(suspect,columns=columns,filters=filters,order_by=orderby,limit=table.limit)
    data['table'] = table
    return render(request, template, data)


def pivot_table(request,template='data_interrogator/pivot.html'):
    from forms import PivotTableForm
    data = {}
    form = PivotTableForm()

    if request.method == 'GET':
        # create a form instance and populate it with data from the request:
        form = PivotTableForm(request.GET)
        # check whether it's valid:
        if form.is_valid():
            # process the data in form.cleaned_data as required
            aggregators = form.cleaned_data.get('aggregators',[])
            columns = [form.cleaned_data.get('column_1'),form.cleaned_data.get('column_2')]
            suspect = form.cleaned_data['lead_suspect']
            filters = form.cleaned_data.get('filter_by',[])

            data = pivot(suspect,columns=columns,filters=filters,aggregators=aggregators)
    data['form']=form
    return render(request, template, data)