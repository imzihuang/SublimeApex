import sublime
import sublime_plugin
import os
import urllib
import json
import threading
import time
import pprint
import urllib.parse
import shutil

from xml.sax.saxutils import unescape
from . import requests, context, util
from .context import COMPONENT_METADATA_SETTINGS
from .util import getUniqueElementValueFromXmlString
from .salesforce import bulkapi, soap_bodies, message
from .salesforce.bulkapi import BulkApi
from .salesforce.bulkapi import BulkJob
from .salesforce.api import SalesforceApi
from .progress import ThreadProgress, ThreadsProgress


def populate_users():
    """
    Get dict (LastName + FirstName => UserId) in whole org

    @return: {
        username + users: {
            LastName + FirstName => UserId
        }
        ...
    }
    """

    # Get username
    toolingapi_settings = context.get_toolingapi_settings()
    username = toolingapi_settings["username"]

    # If sobjects is exist in globals()[], just return it
    if (username + "users") in globals(): 
        return globals()[username + "users"]
    # If sobjects is not exist in globals(), post request to pouplate it
    api = SalesforceApi(toolingapi_settings)
    query = """SELECT Id, FirstName, LastName FROM User WHERE LastName != null 
               AND IsActive = true"""
    thread = threading.Thread(target=api.query_all, args=(query, ))
    thread.start()

    while thread.is_alive() or not api.result:
        time.sleep(1)

    records = api.result["records"]
    users = {}
    for user in records:
        if not user["FirstName"]:
            users[user["LastName"]] = user["Id"]
        else:
            users[user["LastName"] + " " + user["FirstName"]] = user["Id"]

    globals()[username + "users"] = users
    return users  

def populate_components():
    """
    Get all components which NamespacePrefix is null in whole org
    """

    # Get username
    toolingapi_settings = context.get_toolingapi_settings()
    username = toolingapi_settings["username"]

    # If sobjects is exist in globals()[], just return it
    component_metadata = sublime.load_settings("component_metadata.sublime-settings")
    if not component_metadata.has(username): return []

    return_component_attributes = {}
    for component_type in component_metadata.get(username).keys():
        component_attributes = component_metadata.get(username)[component_type]
        for component_name in component_attributes.keys():
            component_id = component_attributes[component_name]["id"]
            component_type = component_attributes[component_name]["type"]
            return_component_attributes[component_type + "-->" + component_name] = component_id

    return return_component_attributes

def populate_classes():
    """
    Get dict (Class Name => Class Id) which NamespacePrefix is null in whole org

    @return: {
        classname: classid
        ...
    }
    """
    # Get username
    toolingapi_settings = context.get_toolingapi_settings()
    username = toolingapi_settings["username"]

    # If sobjects is exist in globals()[], just return it
    component_metadata = sublime.load_settings("component_metadata.sublime-settings")
    if component_metadata.has(username):
        return component_metadata.get(username).get("ApexClass")

    if username + "classes" in globals():
        return globals()[username + "classes"]

    # If sobjects is not exist in globals(), post request to pouplate it
    api = SalesforceApi(toolingapi_settings)
    query = "SELECT Id, Name, Body FROM ApexClass WHERE NamespacePrefix = null"
    thread = threading.Thread(target=api.query_all, args=(query, ))
    thread.start()

    while thread.is_alive() or not api.result:
        time.sleep(1)

    classes = {}
    for record in api.result["records"]:
        name = record["Name"]
        body = record["Body"]
        component_attr = {"id": record["Id"]}
        if "@isTest" in body or "testMethod" in body or "testmethod" in body:
            component_attr["is_test"] = True
        else:
            component_attr["is_test"] = False

        classes[name] = component_attr

    globals()[username + "classes"] = classes
    return classes

def populate_sobject_recordtypes():
    """
    Get dict ([sobject, recordtype name] => recordtype id) in whole org

    @return: {
        username + "sobject_recordtypes": {
            sobject + rtname: rtid
        }
        ...
    }
    """

    # Get username
    toolingapi_settings = context.get_toolingapi_settings()
    username = toolingapi_settings["username"]

    # If sobjects is exist in globals()[], just return it
    if (username + "sobject_recordtypes") in globals(): 
        return globals()[username + "sobject_recordtypes"]

    # If sobjects is not exist in globals(), post request to pouplate it
    api = SalesforceApi(toolingapi_settings)
    query = "SELECT Id, Name, SobjectType FROM RecordType"
    thread = threading.Thread(target=api.query_all, args=(query, ))
    thread.start()

    while thread.is_alive() or not api.result:
        time.sleep(1)

    records = api.result["records"]
    sobject_recordtypes = {}
    for recordtype in records:
        sobject_type = recordtype["SobjectType"]
        recordtype_name = recordtype["Name"]
        recordtype_id = recordtype["Id"]
        sobject_recordtypes[sobject_type + ", " + recordtype_name] = recordtype_id

    # Add Master of every sobject to List
    sobjects_describe = populate_sobjects_describe()
    for sobject_type in sobjects_describe:
        sobject_describe = sobjects_describe[sobject_type]
        if not sobject_describe["layoutable"]: continue
        sobject_recordtypes[sobject_type + ", Master"] = "012000000000000AAA"

    globals()[username + "sobject_recordtypes"] = sobject_recordtypes
    return sobject_recordtypes

def populate_sobjects_describe():
    """
    Get the sobjects list in org.
    """

    # Get username
    toolingapi_settings = context.get_toolingapi_settings()
    username = toolingapi_settings["username"]

    # If sobjects is exist in sobjects_completion.sublime-settings, just return it
    sobjects_completions = sublime.load_settings("sobjects_completion.sublime-settings")
    if sobjects_completions.has(username):
        sobjects_describe = {}
        sd = sobjects_completions.get(username)["sobjects"]
        for key in sd:
            sobject_describe = sd[key]
            sobjects_describe[sobject_describe["name"]] = sobject_describe
        return sobjects_describe

    if (username + "sobjects") in globals():
        return globals()[username + "sobjects"]

    # If sobjects is not exist in globals(), post request to pouplate it
    api = SalesforceApi(toolingapi_settings)
    thread = threading.Thread(target=api.describe_global, args=())
    thread.start()

    while thread.is_alive() or not api.result:
        time.sleep(1)

    sobjects_describe = api.result

    globals()[username + "sobjects"] = sobjects_describe
    return sobjects_describe

def populate_all_test_classes():
    # Get username
    settings = context.get_toolingapi_settings()
    username = settings["username"]

    component_metadata = sublime.load_settings("component_metadata.sublime-settings")
    if not component_metadata.has(username):
        sublime.error_message("No Cache, Please New Project Firstly.")
        return

    classes = component_metadata.get(username)["ApexClass"]
    test_class_ids = []
    for class_name, class_attr in classes.items():
        if not class_attr["is_test"]: continue
        test_class_ids.append(class_attr["id"])

    return test_class_ids

def handle_login_thread(default_project, timeout=120):
    def handle_thread(thread, timeout):
        if thread.is_alive():
            sublime.set_timeout(lambda: handle_thread(thread, timeout), timeout)
            return

        result = api.result
        if result["status_code"] > 399: return
        if toolingapi_settings["output_session_info"]:
            pprint.pprint(result)

        print (message.SEPRATE.format("Login Succeed"))

    toolingapi_settings = context.get_toolingapi_settings()
    api = SalesforceApi(toolingapi_settings)
    thread = threading.Thread(target=api.login, args=(False, ))
    thread.start()
    handle_thread(thread, timeout)
    ThreadProgress(api, thread, "Login to switched project", default_project + " Login Succeed")

def handle_view_code_coverage(component_name, component_attribute, body, timeout=120):
    def handle_thread(thread, timeout):
        if thread.is_alive():
            sublime.set_timeout(lambda: handle_thread(thread, timeout), timeout)
            return

        result = api.result
        if result["status_code"] > 399:
            error_message = "% 20s\t" % "Component Name: "
            error_message += "%-30s\t" % component_name + "\n"
            error_message += util.format_error_message(result)
            print (message.SEPRATE.format(error_message))
            return

        # Show panel
        util.show_panel()

        if result["totalSize"] == 0:
            print (message.SEPRATE.format("You should run test class firstly."))
            return

        view = sublime.active_window().new_file()
        view.run_command("new_view", {
            "name": component_name + " Code Coverage",
            "input": body
        })

        uncovered_lines = result["records"][0]["Coverage"]["uncoveredLines"]
        covered_lines = result["records"][0]["Coverage"]["coveredLines"]
        covered_lines_count = len(covered_lines)
        uncovered_lines_count = len(uncovered_lines)
        total_lines_count = covered_lines_count + uncovered_lines_count
        if total_lines_count == 0:
            print (message.SEPRATE.format("You should run test class firstly."))
            return

        all_region_by_line = view.lines(sublime.Region(0, view.size()))
        uncovered_region = []
        for region in all_region_by_line:
            line = view.rowcol(region.begin() + 1)[0] + 1
            if line in uncovered_lines:
                uncovered_region.append(region)

        view.add_regions("mark", uncovered_region, "bookmark", 'markup.inserted',
            sublime.DRAW_NO_FILL | sublime.DRAW_NO_OUTLINE | sublime.DRAW_STIPPLED_UNDERLINE)

        coverage = covered_lines_count / total_lines_count * 100
        print (message.SEPRATE.format("The coverage is %.2f%%(%s/%s), " %\
            (coverage, covered_lines_count, total_lines_count) + 
            "uncovered lines are marked in the new open view"))

    toolingapi_settings = context.get_toolingapi_settings()
    api = SalesforceApi(toolingapi_settings)
    query = "SELECT Coverage FROM ApexCodeCoverageAggregate " +\
        "WHERE ApexClassOrTriggerId = '{0}'".format(component_attribute["id"])
    thread = threading.Thread(target=api.query, args=(query, True, ))
    thread.start()
    ThreadProgress(api, thread, "View Code Coverage of " + component_name,
        "View Code Coverage of " + component_name + " Succeed")
    handle_thread(thread, timeout)

def handle_refresh_folder(folder_name, component_outputdir, timeout=120):
    def handle_thread(thread, timeout):
        if thread.is_alive():
            sublime.set_timeout(lambda: handle_thread(thread, timeout), timeout)
            return

        result = api.result

        # Output component size
        size = len(result["records"])
        print (message.SEPRATE.format(str(component_type) + " Size: " + str(size)))

        # Write Components to local
        components = {}
        for record in result["records"]:
            # Get Component Name of this record
            component_name = record['Name']
            component_url = record['attributes']['url']
            component_id = record["Id"]
            print (str(component_type) + " ==> " + str(record['Name']))

            # Write mapping of component_name with component_url
            # into component_metadata.sublime-settings
            components[component_name] = {
                "url": component_url,
                "id": component_id,
                "type": component_type,
                "body": component_body,
                "extension": component_extension
            }
            
            # Judge Component is Test Class or not
            body = record[component_body]
            if component_type == "ApexClass":
                if "@isTest" in body or "testMethod" in body or "testmethod" in body:
                    components[component_name]["is_test"] = True
                else:
                    components[component_name]["is_test"] = False

            # Write body to local file
            fp = open(component_outputdir + "/" + component_name +\
                component_extension, "wb")
            try:
                body = bytes(body, "UTF-8")
            except:
                body = body.encode("UTF-8")
            fp.write(body)

            # Set status_message
            sublime.status_message(component_name + " ["  + component_type + "] Downloaded")

        # Save Refreshed Component Attributes to component_metadata.sublime-settings
        s = sublime.load_settings(COMPONENT_METADATA_SETTINGS)
        username = toolingapi_settings["username"]
        components_dict = s.get(username)
        components_dict[component_type] = components
        s.set(username, components_dict)

        sublime.save_settings(context.COMPONENT_METADATA_SETTINGS)

    toolingapi_settings = context.get_toolingapi_settings()
    api = SalesforceApi(toolingapi_settings)

    # Get component attributes by component_type
    component_type = toolingapi_settings[folder_name]
    component_attribute = toolingapi_settings[component_type]
    component_body = component_attribute["body"]
    component_extension = component_attribute["extension"]
    component_soql = component_attribute["soql"]
    thread = threading.Thread(target=api.query_all, args=(component_soql, ))
    thread.start()
    ThreadProgress(api, thread, "Refreshing " + component_type, 
        "Refreshing " + component_type + " Succeed")
    handle_thread(thread, timeout)

def handle_initiate_sobjects_completions(timeout=120):
    """
    Save sobject describe to local which is used in completions

    """

    def handle_threads(apis, threads, timeout):
        for thread in threads:
            if thread.is_alive():
                sublime.set_timeout(lambda: handle_threads(apis, threads, timeout), timeout)
                return
        
        # If succeed, get the all sobject describe result
        results = []
        for api in apis:
            results.append(api.result)

        # Save all sobject describe result to sublime settings
        s = sublime.load_settings("sobjects_completion.sublime-settings")
        sobjects_completion = {"sobjects": {}}

        all_parent_relationship_dict = {}
        all_child_relationship_dict = {}
        display_field_name_and_label = settings["display_field_name_and_label"]
        for sobject_describe in results:
            # Initiate Sobject completions
            if "name" not in sobject_describe: continue
            sobject_name = sobject_describe["name"]

            # If sobject is excluded sobject, just continue
            sobject_name = sobject_name.lower()
            sobjects_completion["sobjects"][sobject_name] = {
                "name": sobject_describe["name"],
                "keyPrefix": sobject_describe["keyPrefix"],
                "layoutable": sobject_describe["layoutable"],
                "triggerable": sobject_describe["triggerable"]
            }

            # Combine Fields dict, Picklist Field dict and parent relationship dict
            fields_dict = {}
            picklist_field_dict = {}
            parent_relationship_dict = {}
            child_relationship_dict = {}
            for f in sobject_describe["fields"]:
                field_name = f["name"]
                precision = f["precision"]
                scale = f["scale"]
                field_type = f["type"]

                field_desc_dict = {
                    "double": "Double(%s, %s)" % (precision, scale),
                    "currency": "Currency(%s, %s)" % (precision, scale),
                    "date": "Date",
                    "datetime": "Datetime",
                    "boolean": "Boolean",
                    "reference": "Reference"
                }

                field_name_desc = "%s(%s)" % (field_name, f["label"]) \
                    if display_field_name_and_label else field_name
                if field_type in field_desc_dict:
                    field_type_desc = field_desc_dict[field_type]
                else:
                    field_type_desc = "%s(%s)" % (field_type.capitalize(), f["length"])

                fd = "%s\t%s" % (field_name_desc, field_type_desc)
                fields_dict[fd] = field_name

                # Picklist Dcit
                if f["type"] == "picklist":
                    picklists = []
                    for picklistValue in f["picklistValues"]:
                        picklists.append({
                            "label": picklistValue["label"],
                            "value": picklistValue["value"]
                        })
                    picklist_field_dict[field_name] = picklists

                # List all Reference Field Relationship Name as fields
                # Some fields has two more references, we can't list the fields of it
                if not len(f["referenceTo"]) == 1: continue
                parentRelationshipName = f["relationshipName"]
                if not parentRelationshipName: continue
                parentSobject = f["referenceTo"][0]
                if parentRelationshipName in all_parent_relationship_dict:
                    is_duplicate = False
                    for so in all_parent_relationship_dict[parentRelationshipName]:
                        if parentSobject == so:
                            is_duplicate = True
                            break

                    if not is_duplicate:
                        all_parent_relationship_dict[parentRelationshipName].append(parentSobject)
                else:
                    all_parent_relationship_dict[parentRelationshipName] = [parentSobject]

                # Add Parent Relationship Name
                parent_relationship_dict[f["relationshipName"]] = parentSobject
            
            # Child Relationship dict
            for f in sobject_describe["childRelationships"]:
                childRelationshipName = f["relationshipName"]
                childSobject = f["childSObject"]
                if not childRelationshipName: continue

                # Add Parent Relationship Name as Field
                child_relationship_dict[childRelationshipName] = childSobject

            # Combine sobject fields dict and sobject child relationship dict
            sobjects_completion["sobjects"][sobject_name]["fields"] = fields_dict
            sobjects_completion["sobjects"][sobject_name]["picklist_fields"] = picklist_field_dict
            sobjects_completion["sobjects"][sobject_name]["parentRelationships"] = parent_relationship_dict
            sobjects_completion["sobjects"][sobject_name]["childRelationships"] = child_relationship_dict

        # Populate Child Relationship and Parent Relationship

        sobjects_completion["parentRelationships"] = all_parent_relationship_dict
        # sobjects_completion["childRelationships"] = all_child_relationship_dict

        # Every project has unique username
        username = settings["username"]
        s.set(username, sobjects_completion)

        # Save settings
        sublime.save_settings("sobjects_completion.sublime-settings")

        # Output message
        print (message.SEPRATE.format('Sobjects completions local history are initiated.'))

    def handle_thread(api, thread, timeout=120):
        if thread.is_alive():
            sublime.set_timeout(lambda:handle_thread(api, thread, timeout), timeout)
            return

        sobjects_describe = api.result
        threads = []
        apis = []
        for sobject in sobjects_describe:
            sobject_describe = sobjects_describe[sobject]
            if sobject in settings["allowed_sobjects"] or sobject_describe["custom"]:
                api = SalesforceApi(settings)
                thread = threading.Thread(target=api.describe_sobject, args=(sobject, ))
                thread.start()
                threads.append(thread)
                apis.append(api)

        ThreadsProgress(threads, "Download Cache of Sobjects", "Download Cache of Sobjects Succeed")
        handle_threads(apis, threads, 10)

    settings = context.get_toolingapi_settings()
    api = SalesforceApi(settings)
    thread = threading.Thread(target=api.describe_global, args=())
    thread.start()
    ThreadProgress(api, thread, "Global Describe", "Global Describe Succeed")
    handle_thread(api, thread, timeout)

def handle_deploy_metadata_thread(zipfile, timeout=120):
    def handle_thread(thread, timeout):
        if thread.is_alive():
            sublime.set_timeout(lambda:handle_thread(thread, timeout), timeout)
            return
        
        result = api.result
        status_code = result["status_code"]
        if status_code > 399: return
        print (message.SEPRATE.format("Deploy Metadata Succeed"))

    toolingapi_settings = context.get_toolingapi_settings()
    api = SalesforceApi(toolingapi_settings)
    thread = threading.Thread(target=api.deploy_metadata, args=(zipfile, ))
    thread.start()
    ThreadProgress(api, thread, "Deploy Metadata", "Deploy Metadata Succeed")
    handle_thread(thread, timeout)

def handle_close_jobs_thread(job_ids, timeout=120):
    settings = context.get_toolingapi_settings()
    bulkjob = BulkJob(settings, None, None)
    for job_id in job_ids:
        thread = threading.Thread(target=bulkjob.close_job, args=(job_id,))
        thread.start()

def handle_bulk_operation_thread(sobject, inputfile, operation, timeout=120):
    settings = context.get_toolingapi_settings()
    bulkapi = BulkApi(settings, sobject, inputfile)
    if operation == "insert":
        target = bulkapi.insert
    elif operation == "update":
        target = bulkapi.update
    elif operation == "upsert":
        target = bulkapi.upsert
    elif operation == "delete":
        target = bulkapi.delete
    thread = threading.Thread(target=target, args=())
    thread.start()
    progress_message = operation + " " + sobject
    ThreadProgress(bulkapi, thread, progress_message, progress_message + " Succeed")

def handle_backup_sobject_thread(sobject, soql=None, timeout=120):
    settings = context.get_toolingapi_settings()
    bulkapi = BulkApi(settings, sobject, soql)
    thread = threading.Thread(target=bulkapi.query, args=())
    thread.start()
    wait_message = "Export Records of " + sobject
    ThreadProgress(bulkapi, thread, wait_message, wait_message + " Succeed")

def handle_backup_all_sobjects_thread(timeout=120):
    def handle_thread(thread, timeout):
        if thread.is_alive():
            sublime.set_timeout(lambda:handle_thread(thread, timeout), timeout)
            return

        sobjects_describe = api.result
        threads = []
        for sobject in sobjects_describe:
            bulkapi = BulkApi(settings, sobject)
            thread = threading.Thread(target=bulkapi.query, args=())
            thread.start()
            threads.append(thread)

        wait_message = "Export All Sobjects Records"
        ThreadsProgress(threads, wait_message, wait_message + " Succeed")

    settings = context.get_toolingapi_settings()
    api = SalesforceApi(settings)
    thread = threading.Thread(target=api.describe_global, args=())
    thread.start()
    ThreadProgress(api, thread, "Describe Global", "Describe Global Succeed")
    handle_thread(thread, timeout)

def handle_retrieve_all_thread(timeout=120, retrieve_all=True):
    def handle_thread(thread, timeout):
        if thread.is_alive():
            sublime.set_timeout(lambda:handle_thread(thread, timeout), timeout)
            return
        
        if not api.result: return
        if api.result["status_code"] > 399: return

        # Mkdir for output dir of zip file
        result = api.result
        context.add_project_to_workspace(toolingapi_settings["workspace"])
        outputdir = toolingapi_settings["workspace"] + "/metadata"
        if not os.path.exists(outputdir):
            os.makedirs(outputdir)

        # Extract zip
        util.extract_zip(result["zipFile"], outputdir)

        # Remove this zip file
        # os.remove(zipdir)

        # Output package path
        success_message = message.SEPRATE.format("Metadata are exported to: " + outputdir)
        view = util.get_view_by_name("Progress Monitor: Retrieve Metadata")
        view.run_command("new_dynamic_view", {
            "view_id": view.id(),
            "view_name": "Progress Monitor: Retrieve Metadata",
            "input": success_message,
            "point": view.size()
        })
        sublime.status_message("Exported Path: " + outputdir)

    toolingapi_settings = context.get_toolingapi_settings()
    api = SalesforceApi(toolingapi_settings)

    if retrieve_all:
        soap_body = soap_bodies.retrieve_all_task_body
    else:
        soap_body = soap_bodies.retrieve_sobjects_workflow_task_body

    thread = threading.Thread(target=api.retrieve, args=(soap_body, ))
    thread.start()
    ThreadProgress(api, thread, "Retrieve Metadata", "Retrieve Metadata Succeed")
    handle_thread(thread, timeout)

def handle_export_workflows(timeout=120):
    def handle_thread(thread, timeout):
        if thread.is_alive():
            sublime.set_timeout(lambda: handle_thread(thread, timeout), timeout)
            return
        
        # If succeed
        sobjects = api.result.keys()
        util.parse_workflow_metadata(toolingapi_settings, sobjects)
        print (message.SEPRATE.format("Outputdir: " + outputdir))

    toolingapi_settings = context.get_toolingapi_settings()
    outputdir = toolingapi_settings["workspace"] + "/workflow/"
    api = SalesforceApi(toolingapi_settings)
    thread = threading.Thread(target=api.describe_global, args=())
    thread.start()
    ThreadProgress(api, thread, "Export All Workflows", "Outputdir: " + outputdir)
    handle_thread(thread, 10)

def handle_export_validation_rules(timeout=120):
    def handle_thread(thread, timeout):
        if thread.is_alive():
            sublime.set_timeout(lambda: handle_thread(thread, timeout), timeout)
            return

        # If succeed
        sobjects = api.result.keys()
        util.parse_validation_rule(toolingapi_settings, sobjects)
        print (message.SEPRATE.format("Outputdir: " + outputdir))

    toolingapi_settings = context.get_toolingapi_settings()
    outputdir = toolingapi_settings["workspace"] + "/validation/validation rules.csv"
    api = SalesforceApi(toolingapi_settings)
    thread = threading.Thread(target=api.describe_global, args=())
    thread.start()
    ThreadProgress(api, thread, "Export All Validation Rules", "Outputdir: " + outputdir)
    handle_thread(thread, 10)

def handle_export_customfield(timeout=120):
    def handle_thread(thread, timeout):
        if thread.is_alive():
            sublime.set_timeout(lambda: handle_thread(thread, timeout), timeout)
            return
        
        # If succeed
        result = api.result
        if result["status_code"] > 399 : return

        # Write list to csv
        if not os.path.exists(outputdir): os.makedirs(outputdir)
        records = sorted(result["records"], key=lambda k : k['TableEnumOrId'])
        util.list2csv(outputdir + "/customfield.csv", records)

        # Output log
        print (message.SEPRATE.format(outputdir))

    toolingapi_settings = context.get_toolingapi_settings()
    workspace = context.get_toolingapi_settings().get("workspace")
    outputdir = workspace + "/customfield"
    api = SalesforceApi(toolingapi_settings)
    query = "SELECT Id,TableEnumOrId,DeveloperName,NamespacePrefix,FullName FROM CustomField"
    thread = threading.Thread(target=api.query, args=(query, True,))
    thread.start()
    ThreadProgress(api, thread, 'Describe CustomField', 
        'Outputdir: ' + outputdir + "/customfield.csv")
    handle_thread(thread, 10)

def handle_export_data_template_thread(sobject, recordtype_name, recordtype_id, timeout=120):
    def handle_thread(thread, timeout):
        if thread.is_alive():
            sublime.set_timeout(lambda: handle_thread(thread, timeout), timeout)
            return
        
        # If succeed
        result = api.result
        if result["status_code"] > 399 : return

        # If outputdir is not exist, just make it
        if not os.path.exists(outputdir): os.makedirs(outputdir)

        # Write parsed result to csv
        util.parse_data_template(output_file_dir, result)
        print (message.SEPRATE.format("Data Template outputdir: " + output_file_dir))

    toolingapi_settings = context.get_toolingapi_settings()
    outputdir = toolingapi_settings["workspace"] + "/template"
    output_file_dir = outputdir + "/" + sobject + "-" + recordtype_name + ".csv"
    api = SalesforceApi(toolingapi_settings)
    url = "/sobjects/%s/describe/layouts/%s" % (sobject, recordtype_id)
    thread = threading.Thread(target=api.get, args=(url, ))
    thread.start()
    wait_message = "Export Data Template of %s=>%s" % (sobject, recordtype_name)
    ThreadProgress(api, thread, wait_message, "Outputdir: " + output_file_dir)
    handle_thread(thread, 120)

def handle_execute_rest_test(operation, url, data=None, timeout=120):
    def handle_new_view_thread(thread, timeout):
        if thread.is_alive():
            sublime.set_timeout(lambda: handle_new_view_thread(thread, timeout), timeout)
            return
        
        # If succeed
        result = api.result
        if "list" in result: result = result["list"]
        if "str"  in result: result = result["str"]
        
        # No error, just display log in a new view
        view = sublime.active_window().new_file()
        view.set_syntax_file("Packages/JavaScript/JavaScript.tmLanguage")
        view.run_command("new_view", {
            "name": "Execute Rest %s Result" % operation,
            "input": pprint.pformat(result)
        })

    toolingapi_settings = context.get_toolingapi_settings()
    api = SalesforceApi(toolingapi_settings)
    http_methods_target = {
        "Get": api.get,
        "Delete": api.delete,
        "Head": api.head,
        "Put": api.put,
        "Post": api.post,
        "Query": api.query,
        "Tooling Query": api.query,
        "Query All": api.query_all,
        "Retrieve Body": api.retrieve_body,
        "Patch": api.patch,
        "Search": api.search,
        "Quick Search": api.quick_search
    }
    
    target = http_methods_target[operation]
    if operation in ['Put', 'Post', 'Patch']:
        thread = threading.Thread(target=target, args=(url, data,))
    elif operation == "Tooling Query":
        thread = threading.Thread(target=target, args=(url, True))
    else:
        thread = threading.Thread(target=target, args=(url,))
    thread.start()
    progress_message = "Execute Rest %s Test" % operation
    ThreadProgress(api, thread, progress_message, progress_message + " Succeed", open_console=False)
    handle_new_view_thread(thread, timeout)

def handle_execute_query(soql, timeout=120):
    def handle_new_view_thread(thread, timeout):
        if thread.is_alive():
            sublime.set_timeout(lambda: handle_new_view_thread(thread, timeout), timeout)
            return
        
        # If succeed
        result = api.result
        if result["status_code"] > 399: return
        
        # No error, just display log in a new view
        view = sublime.active_window().new_file()
        view.run_command("new_view", {
            "name": "Execute Query Result",
            "input": pprint.pformat(result)
        })

        # Keep the history in the local history rep
        util.add_operation_history('execute_query', soql)

    settings = context.get_toolingapi_settings()
    api = SalesforceApi(settings)
    thread = threading.Thread(target=api.query, args=(soql,))
    thread.start()
    ThreadProgress(api, thread, "Execute Query", "Execute Query Succeed")
    handle_new_view_thread(thread, timeout)

def handle_execute_anonymous(apex_string, timeout=120):
    def handle_new_view_thread(thread, timeout):
        if thread.is_alive():
            sublime.set_timeout(lambda: handle_new_view_thread(thread, timeout), timeout)
            return
        
        # If succeed
        result = api.result
        if result["status_code"] > 399: return

        # No error, just display log in a new view
        view = sublime.active_window().new_file()
        view.run_command("new_view", {
            "name": "Execute Anonymous Result",
            "input": util.parse_execute_anonymous_xml(result)
        })

        # Keep the history apex script to local
        util.add_operation_history('execute_anonymous', apex_string)

    settings = context.get_toolingapi_settings()
    api = SalesforceApi(settings)
    thread = threading.Thread(target=api.execute_anonymous, args=(apex_string, ))
    thread.start()
    ThreadProgress(api, thread, "Execute Anonymous", "Execute Anonymous Succeed")
    handle_new_view_thread(thread, timeout)

def handle_fetch_logs(user_full_name, user_id, timeout=120):
    def handle_thread(thread, timeout):
        if thread.is_alive():
            sublime.set_timeout(lambda: handle_thread(thread, timeout), timeout)
            return

        result = api.result
        records = result["records"]
        debug_logs_table = util.format_debug_logs(settings, records)
        view = sublime.active_window().new_file()
        view.run_command("new_view", {
            "name": "Debug Logs",
            "input": debug_logs_table
        })

    settings = context.get_toolingapi_settings()
    api = SalesforceApi(settings)
    query = "SELECT Id,LogUserId,LogLength,Request,Operation,Application," +\
            "Status,DurationMilliseconds,StartTime,Location FROM ApexLog " +\
            "WHERE LogUserId='%s' ORDER BY StartTime LIMIT %s" % (user_id, settings["last_n_logs"])
    print (query)
    thread = threading.Thread(target=api.query_all, args=(query, ))
    thread.start()
    ThreadProgress(api, thread, "List Debug Logs for " + user_full_name, 
        "List Debug Logs for " + user_full_name + " Succeed")
    handle_thread(thread, timeout)    

def handle_create_debug_log(user_name, user_id, timeout=120):
    def handle_thread(thread, timeout):
        if thread.is_alive():
            sublime.set_timeout(lambda: handle_thread(thread, timeout), timeout)
            return

        result = api.result
        if result["status_code"] > 399: return
        print (message.SEPRATE.format(user_name + " " + result["message"]) )

    toolingapi_settings = context.get_toolingapi_settings()
    api = SalesforceApi(toolingapi_settings)
    thread = threading.Thread(target=api.create_trace_flag, args=(user_id, ))
    thread.start()
    ThreadProgress(api, thread, "Create Debug Log for " + user_name, 
        "Create Debug Log for " + user_name + " Succeed")
    handle_thread(thread, timeout)

def handle_view_debug_log_detail(log_id, timeout=120):
    def handle_thread(thread, timeout):
        if thread.is_alive():
            sublime.set_timeout(lambda: handle_thread(thread, timeout), timeout)
            return
        
        if api.result["status_code"] > 399: return
        view = sublime.active_window().new_file()
        view.run_command("new_view", {
            "name": "Debug Log Detail",
            "input": api.result["body"]
        })

    toolingapi_settings = context.get_toolingapi_settings()
    api = SalesforceApi(toolingapi_settings)
    url = "/sobjects/ApexLog/" + log_id + "/Body"
    thread = threading.Thread(target=api.retrieve_body, args=(url, ))
    thread.start()
    ThreadProgress(api, thread, "Get Log Detail of " + log_id, 
        "Get Log Detail of " + log_id + " Succeed")
    handle_thread(thread, timeout)

def handle_run_all_test(timeout=120):
    def handle_threads(api_threads, timeout):
        for api, thread in api_threads:
            if thread.is_alive():
                sublime.set_timeout(lambda: handle_threads(api_threads, timeout), timeout)
                return
            else:
                result = api.result
                if "status_code" in result and result["status_code"] > 399: continue

                # No error, just display log in a new view
                test_result = util.parse_test_result(result)
                view = util.get_view_by_name("Run All Test Result")
                if not view:
                    view = sublime.active_window().new_file()
                    view.run_command("new_dynamic_view", {
                        "view_id": view.id(),
                        "view_name": "Run All Test Result",
                        "input": util.parse_test_result(result) + "\n" * 4 + "*" * 100
                    })
                else:
                    view.run_command("new_dynamic_view", {
                        "view_id": view.id(),
                        "view_name": "Run All Test Result",
                        "input": "\n" + util.parse_test_result(result) + "\n" * 4 + "*" * 100,
                        "point": view.size()
                    })

                api_threads.remove((api, thread))

        # After run test succeed, get ApexCodeCoverageAggreate
        query = "SELECT ApexClassOrTrigger.Name, NumLinesCovered, NumLinesUncovered, Coverage " +\
                "FROM ApexCodeCoverageAggregate"
        api = SalesforceApi(toolingapi_settings)
        thread = threading.Thread(target=api.query, args=(query, True, ))
        thread.start()
        wait_message = "Get Code Coverage of All Class"
        ThreadProgress(api, thread, wait_message, wait_message + " Succeed")
        handle_code_coverage_thread(thread, api, view, timeout)

    def handle_code_coverage_thread(thread, api, view, timeout=120):
        if thread.is_alive():
            sublime.set_timeout(lambda: handle_code_coverage_thread(thread, api, view, timeout), timeout)
            return

        code_coverage = util.parse_code_coverage(api.result)
        view.run_command("new_dynamic_view", {
            "view_id": view.id(),
            "view_name": "Run All Test Result",
            "input": code_coverage,
            "point": view.size()
        })

    class_ids = populate_all_test_classes()
    if not class_ids: return

    toolingapi_settings = context.get_toolingapi_settings()

    api_threads = []
    threads = []
    for class_id in class_ids:
        api = SalesforceApi(toolingapi_settings)
        thread = threading.Thread(target=api.run_test, args=(class_id, ))
        threads.append(thread)
        api_threads.append((api, thread))
        thread.start()
    ThreadsProgress(threads, "Run All Test", "Run All Test Succeed")
    handle_threads(api_threads, timeout)


def handle_run_test(class_name, class_id, timeout=120):
    def handle_thread(thread, timeout):
        if thread.is_alive():
            sublime.set_timeout(lambda: handle_thread(thread, timeout), timeout)
            return

        # If succeed
        result = api.result

        # If error
        if "status_code" in result and result["status_code"] > 399: return

        # No error, just display log in a new view
        test_result = util.parse_test_result(result)
        class_name = result[0]["ApexClass"]["Name"]
        view = sublime.active_window().new_file()
        view.run_command("new_dynamic_view", {
            "view_id": view.id(),
            "view_name": "Test Result",
            "input": test_result
        })
        
        # Keep the history in the local history rep
        util.add_operation_history('test/' + class_name, test_result)

        # After run test succeed, get ApexCodeCoverageAggreate
        query = "SELECT ApexClassOrTrigger.Name, NumLinesCovered, NumLinesUncovered, Coverage " +\
                "FROM ApexCodeCoverageAggregate"
        thread = threading.Thread(target=api.query, args=(query, True, ))
        thread.start()
        wait_message = "Get Code Coverage of " + class_name
        ThreadProgress(api, thread, wait_message, wait_message + " Succeed")
        handle_code_coverage_thread(thread, view, timeout)

    def handle_code_coverage_thread(thread, view, timeout):
        if thread.is_alive():
            sublime.set_timeout(lambda: handle_code_coverage_thread(thread, view, timeout), timeout)
            return

        code_coverage = util.parse_code_coverage(api.result)
        view.run_command("new_dynamic_view", {
            "view_id": view.id(),
            "view_name": "Test Result",
            "input": code_coverage,
            "point": view.size()
        })

    settings = context.get_toolingapi_settings()
    api = SalesforceApi(settings)
    thread = threading.Thread(target=api.run_test, args=(class_id, ))
    thread.start()
    ThreadProgress(api, thread, "Run Test Class " + class_name, "Run Test for " + class_name + " Succeed")
    handle_thread(thread, timeout)

def handle_run_sync_test_classes(class_names, timeout=120):
    def handle_new_view_thread(thread, timeout):
        if thread.is_alive():
            sublime.set_timeout(lambda: handle_new_view_thread(thread, timeout), timeout)
            return
        elif not api.result:
            return

        # If succeed
        result = api.result
        pprint.pprint(result)
        pprint.pprint(util.parse_code_coverage(result))

    settings = context.get_toolingapi_settings()
    api = SalesforceApi(settings)
    thread = threading.Thread(target=api.run_tests_synchronous, args=(class_names, ))
    thread.start()
    wait_message = 'Run Sync Test Classes for Specified Test Class'
    ThreadProgress(api, thread, wait_message, wait_message + ' Succeed')
    handle_new_view_thread(thread, timeout)

def handle_run_async_test_classes(class_ids, timeout=120):
    def handle_new_view_thread(thread, timeout):
        if thread.is_alive():
            sublime.set_timeout(lambda: handle_new_view_thread(thread, timeout), timeout)
            return
        elif not api.result:
            return

        # If succeed
        result = api.result
        pprint.pprint(result)

    settings = context.get_toolingapi_settings()
    api = SalesforceApi(settings)
    thread = threading.Thread(target=api.run_tests_asynchronous, args=(class_ids, ))
    thread.start()
    wait_message = 'Run Sync Test Classes for Specified Test Class'
    ThreadProgress(api, thread, wait_message, wait_message + ' Succeed')
    handle_new_view_thread(thread, timeout)

def handle_generate_sobject_soql(sobject, timeout=120):
    def handle_new_view_thread(thread, timeout):
        if thread.is_alive():
            sublime.set_timeout(lambda: handle_new_view_thread(thread, timeout), timeout)
            return

        # If succeed
        result = api.result
        
        # Error Message are prcoessed in ThreadProgress
        if result["status_code"] > 399: return

        # No error, just display log in a new view
        view = sublime.active_window().new_file()
        view.run_command("new_view", {
            "name": sobject + " SOQL",
            "input": result["soql"]
        })

        # Keep sobject describe history
        util.add_operation_history('soql/' + sobject, result["soql"])

    settings = context.get_toolingapi_settings()
    api = SalesforceApi(settings)
    thread = threading.Thread(target=api.combine_soql, args=(sobject, ))
    thread.start()
    wait_message = 'Generate SOQL for ' + sobject
    ThreadProgress(api, thread, wait_message, wait_message + ' Succeed')
    handle_new_view_thread(thread, timeout)

def handle_describe_sobject(sobject, timeout=120):
    def handle_new_view_thread(thread, timeout):
        if thread.is_alive():
            sublime.set_timeout(lambda: handle_new_view_thread(thread, timeout), timeout)
            return

        # If succeed
        result = api.result
        
        # Error Message are prcoessed in ThreadProgress
        if result["status_code"] > 399: return

        # No error, just display log in a new view
        view = sublime.active_window().new_file()
        describe_result = util.parse_sobject_field_result(result)
        view.run_command("new_view", {
            "name": sobject + " Describe Result",
            "input": describe_result
        })

        # Keep sobject describe history
        util.add_operation_history('describe/' + sobject, describe_result)

    toolingapi_settings = context.get_toolingapi_settings()
    api = SalesforceApi(toolingapi_settings)
    sobject_url = "/sobjects/" + sobject + "/describe"
    thread = threading.Thread(target=api.get, args=(sobject_url, ))
    thread.start()
    ThreadProgress(api, thread, 'Describe ' + sobject, 'Describe ' + sobject + ' Succeed')
    handle_new_view_thread(thread, timeout)

def handle_generate_specified_workbooks(sobjects, timeout=120):
    toolingapi_settings = context.get_toolingapi_settings()
    api = SalesforceApi(toolingapi_settings)
    threads = []
    for sobject in sobjects:
        thread = threading.Thread(target=api.generate_workbook, args=(sobject, ))
        threads.append(thread)
        thread.start()

    ThreadsProgress(threads, "Generating Sobjects Workbook", 
        "Sobjects Workbook are Generated")

def handle_generate_all_workbooks(timeout=120):
    def handle_thread(thread, timeout):
        if thread.is_alive():
            sublime.set_timeout(lambda: handle_thread(thread, timeout), timeout)
            return
        
        # If succeed
        sobjects_describe = api.result
        for sobject in sobjects_describe:
            thread = threading.Thread(target=api.generate_workbook, args=(sobject, ))
            thread.start()

    toolingapi_settings = context.get_toolingapi_settings()
    api = SalesforceApi(toolingapi_settings)
    thread = threading.Thread(target=api.describe_global, args=())
    thread.start()
    ThreadProgress(api, thread, "Global Describe Common", "Global Describe Common Succeed")
    handle_thread(thread, timeout)

def handle_new_project(settings, timeout=120):
    def handle_thread(thread, timeout):
        if thread.is_alive():
            sublime.set_timeout(lambda: handle_thread(thread, timeout), timeout)
            return
        
        # If succeed, something may happen,
        # for example, user password is expired
        result = api.result
        if not result: return
        if "status_code" in result and result["status_code"] > 399: return

        # Load COMPONENT_METADATA_SETTINGS Settings and put all result into it
        # Every org has one local repository
        component_metadata = result
        component_settings = sublime.load_settings(COMPONENT_METADATA_SETTINGS)
        component_settings.set(settings["username"], component_metadata)
        sublime.save_settings(COMPONENT_METADATA_SETTINGS)
        print (message.SEPRATE.format('All code are Downloaded.'))
        sublime.status_message("Refresh All Successfully")

        # After Refresh all succeed, start initiate sobject completions
        handle_initiate_sobjects_completions(120)

        # If get_static_resource_body is true, 
        # start to get all binary body of static resource
        if settings["get_static_resource_body"]:
            folder_name = settings["StaticResource"]["folder"]
            handle_get_static_resource_body(folder_name)

    api = SalesforceApi(settings)
    component_types = settings["component_types"]
    thread = threading.Thread(target=api.refresh_components, args=(component_types, ))
    thread.start()
    ThreadProgress(api, thread, "Initiate Project, Please Wait...", "New Project Succeed")
    handle_thread(thread, timeout)

def handle_get_static_resource_body(folder_name, static_resource_dir=None, timeout=120):
    def handle_thread(thread, static_resource_dir, timeout):
        if thread.is_alive():
            sublime.set_timeout(lambda:handle_thread(thread, static_resource_dir, timeout), timeout)
            return
        
        if not api.result: return
        if api.result["status_code"] > 399: return

        # Mkdir for output dir of zip file
        result = api.result
        if not static_resource_dir:
            static_resource_dir = settings["workspace"] + folder_name
        if not os.path.exists(static_resource_dir): os.makedirs(static_resource_dir)

        # Extract zip
        util.extract_zip(result["zipFile"], static_resource_dir)

        # Move the file to staticresources path
        root_src_dir = static_resource_dir + "/unpackaged/staticresources"
        root_dst_dir = static_resource_dir
        for x in os.walk(root_src_dir):
            if not x[-1]: continue
            for _file in x[-1]:
                if not _file.endswith("resource"): continue
                if os.path.exists(root_dst_dir + '/' + _file):
                    os.remove(root_dst_dir + '/' + _file)
                os.rename(x[0] + '/' + _file, root_dst_dir + '/' + _file) 

        shutil.rmtree(static_resource_dir + "/unpackaged", ignore_errors=True)
        os.remove(static_resource_dir + "/package.zip")

    settings = context.get_toolingapi_settings()
    api = SalesforceApi(settings)
    thread = threading.Thread(target=api.retrieve, 
        args=(soap_bodies.retrieve_static_resources_body, ))
    thread.start()
    handle_thread(thread, static_resource_dir, timeout)
    ThreadProgress(api, thread, "Retrieve StaticResource", "Retrieve StaticResource Succeed")

def handle_save_component(component_name, component_attribute, body, is_check_only=False, timeout=120):
    def handle_thread(thread, timeout):
        if thread.is_alive():
            sublime.set_timeout(lambda:handle_thread(thread, timeout), timeout)
            return

        # Set Thread alive flag to False
        globals()[username + component_name] = False

        # Process request result
        result = api.result
        extension = component_attribute["extension"]
        file_base_name =  component_name + extension
        if "success" in result and result["success"]:
            if "symbol_table" in result:
                # Save symbolTable to component_metadata.sublime-settings
                s = sublime.load_settings("symbol_table.sublime-settings")
                components_dict = {}
                components_dict = s.get(username) if s.has(username) else {}
                components_dict[component_name.lower()] = result["symbol_table"]

                s.set(username, components_dict)
                sublime.save_settings("symbol_table.sublime-settings")

            # Output succeed message in the console
            print (message.SEPRATE.format(
                "{0} is saved successfully at {1}".format(file_base_name, 
                    time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time())))))

        # If not succeed, just go to the error line
        # Because error line in page is always at the line 1, so just work in class or trigger
        elif "success" in result and not result["success"]:
            view = sublime.active_window().active_view()
            if file_base_name in view.file_name() and extension in [".trigger", ".cls"]:
                line = result["line"] if "line" in result else result["lineNumber"]
                if isinstance(line, list): line = line[0]
                view.run_command("goto_line", {"line": line})
                view.run_command("expand_selection", {"to":"line"})

                # Add highlight for error line and remove the highlight after several seconds
                component_id = component_attribute["id"]
                view.run_command("set_check_point", {"mark":component_id+"error"})
                sublime.set_timeout_async(view.run_command("remove_check_point", 
                    {"mark":component_id+"error"}), 
                    settings["delay_seconds_for_hidden_console"] * 1000)

    # If saving is in process, just skip
    settings = context.get_toolingapi_settings()
    username = settings["username"]
    if username + component_name in globals():
        is_thread_alive = globals()[username + component_name]
        if is_thread_alive:
            print ('%s is in process' % component_name);
            return

    api = SalesforceApi(settings)
    thread = threading.Thread(target=api.save_component,
        args=(component_attribute, body, is_check_only, ))
    thread.start()

    # If saving thread is started, set the flag to True
    globals()[username + component_name] = True

    # Display thread progress
    wait_message = ("Compiling " if is_check_only else "Saving ") + component_name
    ThreadProgress(api, thread, wait_message, wait_message + " Succeed")
    handle_thread(thread, timeout)

def handle_create_component(data, component_name, component_type, file_name, timeout=120):
    def handle_thread(thread, timeout):
        if thread.is_alive():
            sublime.set_timeout(lambda:handle_thread(thread, timeout), timeout)
            return
        
        # If create Succeed
        result = api.result

        # If created failed, just remove it
        if result["status_code"] > 399:
            os.remove(file_name)
            return

        # If created succeed, just open it
        sublime.active_window().open_file(file_name)

        # Get the created component id
        component_id = result.get("id")
        body = toolingapi_settings[component_type]["body"]
        extension = toolingapi_settings[component_type]["extension"]
        
        # Save it to component.sublime-settings
        s = sublime.load_settings(COMPONENT_METADATA_SETTINGS)
        username = toolingapi_settings["username"]
        components_dict = s.get(username)
        components_dict[component_type][component_name] = {
            "id": component_id,
            "url": post_url + "/" + component_id,
            "body": body,
            "extension": extension,
            "type": component_type,
            "is_test": False
        }
        s.set(username, components_dict)

        # Save settings and show success message
        sublime.save_settings(COMPONENT_METADATA_SETTINGS)
        file_base_name = component_name + extension
        print (message.SEPRATE.format(
            "{0} is created successfully at {1}".format(file_base_name, 
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time())))))
                
    toolingapi_settings = context.get_toolingapi_settings()
    api = SalesforceApi(toolingapi_settings)
    post_url = "/sobjects/" + component_type
    thread = threading.Thread(target=api.post, args=(post_url, data, ))
    thread.start()
    ThreadProgress(api, thread, "Creating Component " + component_name, 
        "Creating Component " + component_name + " Succeed")
    handle_thread(thread, timeout)

def handle_refresh_static_resource(component_attribute, file_name, timeout=120):
    def handle_thread(thread, timeout):
        if thread.is_alive():
            sublime.set_timeout(lambda:handle_thread(thread, timeout), timeout)
            return
        
        result = api.result

        # If error, just skip, error is processed in ThreadProgress
        if result["status_code"] > 399: return

        fp = open(file_name, "wb")
        fp.write(bytes(result["body"], "utf-8"))

    toolingapi_settings = context.get_toolingapi_settings()
    api = SalesforceApi(toolingapi_settings)
    url = component_attribute["url"] + "/body"
    thread = threading.Thread(target=api.retrieve_body, args=(url, ))
    thread.start()
    ThreadProgress(api, thread, 'Refresh StaticResource', 'Refresh StaticResource Succeed')
    handle_thread(thread, timeout)

def handle_refresh_component(component_attribute, file_name, timeout=120):
    def handle_thread(thread, timeout):
        if thread.is_alive():
            sublime.set_timeout(lambda:handle_thread(thread, timeout), timeout)
            return
        
        result = api.result
        status_code = result["status_code"]
        
        # If error, just skip, error is processed in ThreadProgress
        if status_code > 399: return

        fp = open(file_name, "wb")
        try:
            body = bytes(result[component_body], "UTF-8")
        except:
            body = result[component_body].encode("UTF-8")

        fp.write(body)

    toolingapi_settings = context.get_toolingapi_settings()
    api = SalesforceApi(toolingapi_settings)
    component_body = component_attribute["body"]
    component_url = component_attribute["url"]
    thread = threading.Thread(target=api.get, args=(component_url, ))
    thread.start()
    ThreadProgress(api, thread, 'Refresh Component', 'Refresh Succeed')
    handle_thread(thread, timeout)

def handle_delete_component(component_url, file_name, timeout=120):
    def handle_thread(thread, timeout):
        if thread.is_alive():
            sublime.set_timeout(lambda:handle_thread(thread, timeout), timeout)
            return

        # If succeed
        result = api.result
        if result["status_code"] > 399: return
        os.remove(file_name)
        sublime.active_window().run_command("close")

        print (message.SEPRATE.format(
            "{0} is deleted successfully at {1}".format(file_name, 
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time())))))

    toolingapi_settings = context.get_toolingapi_settings()
    api = SalesforceApi(toolingapi_settings)
    thread = threading.Thread(target=api.delete, args=(component_url, ))
    thread.start()
    file_base_name = os.path.basename(file_name)
    ThreadProgress(api, thread, "Deleting " + file_base_name,
        "Delete " + file_base_name + " Succeed")
    handle_thread(thread, timeout)