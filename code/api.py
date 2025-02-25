from fastapi import FastAPI, Request, HTTPException, UploadFile, Depends
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, FileResponse, StreamingResponse
import psutil
from pydantic import BaseModel
import re
import os
import subprocess
from typing import List
import logging
import shutil
import json

from dotenv import load_dotenv
load_dotenv(override=True)

# setup logging
from log_utils import setup_logger
setup_logger()

import utils.cosmos_helpers as cs
from doc_utils import search, generate_section, write_to_file
from processor import read_asset_file, gpt4_models
from utils.cogsearch_rest import CogSearchHttpRequest, CogSearchRestAPI
from aml_job import AmlJob
from env_vars import ROOT_PATH_INGESTION
from utils.ingestion_cosmos_helper import IngestionCosmosHelper
import threading
    
# Ensure all doc_utils.logc calls are redirected to the append_log_message function
import utils.logc
import asyncio
import threading

# Global setup
LOG_CONTAINER_NAME = os.environ.get("COSMOS_LOG_CONTAINER")

app = FastAPI()
cosmos = cs.SCCosmosClient()
aml_job = AmlJob()
cosmos_log = cs.SCCosmosClient(container_name=LOG_CONTAINER_NAME)
ic = IngestionCosmosHelper()
file_pattern = re.compile(r'system_prompt_ver_(\d+)\.txt')


def get_latest_file_version(directory, file_pattern):
    max_version = -1
    latest_file = None

    for filename in os.listdir(directory):
        match = file_pattern.match(filename)
        if match:
            version = int(match.group(1))
            if version > max_version:
                max_version = version
                latest_file = filename

    return os.path.join(directory, latest_file) if latest_file else None

# FastAPI global configuration
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc):
    logging.error(f"Unprocessable request: {request} {exc}")
    return JSONResponse(
        status_code=422,
        content={"detail": exc.errors(), "body": exc.body},
    )

# API Endpoints

# A GET operation to get job runners
@app.get("/job_runners")
def get_job_runners():
    try:
        logging.info("Getting job runners")
        list = []
        if os.environ.get("AML_RESOURCE_GROUP") and os.environ.get("AML_SUBSCRIPTION_ID") and os.environ.get("AML_WORKSPACE_NAME"):
            list.append("Azure Machine Learning")
        if os.environ.get("INGESTION_JOB_NAME"):
            list.append("Container App Job")
        if os.environ.get("LOCAL_TESTING"):
            list.append("Subprocess (Local Testing)")
        return list
    except Exception as e:
        logging.error(f"Error getting job runners: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

# A GET operation to get all prompts
@app.get("/prompt")
def get_prompts():
    try:
        logging.info("Getting all prompts")
        return cosmos.get_all_documents()
    except Exception as e:
        logging.error(f"Error getting prompts: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

# A PATCH operation to upsert a prompt
@app.patch("/prompt")
async def upsert_prompt(prompt: Request):
    try:
        logging.info("Upserting a prompt")
        doc = await prompt.json()
        return cosmos.upsert_document(doc)
    except Exception as e:
        logging.error(f"Error upserting prompt: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

# a POST operation to create a new prompt
@app.post("/prompt")
async def create_prompt(prompt: Request):
    try:
        logging.info("Creating a new prompt")
        doc = await prompt.json()
        return cosmos.create_document(doc)
    except Exception as e:
        logging.error(f"Error creating prompt: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

# A DELETE operation to delete a prompt
@app.delete("/prompt/{prompt_id}")
def delete_prompt(prompt_id):
    try:
        logging.info("Deleting a prompt")
        return cosmos.delete_document(prompt_id)
    except Exception as e:
        logging.error(f"Error deleting prompt: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

# A GET operation to get a specific prompt
@app.get("/prompt/{prompt_id}", response_class=PlainTextResponse)
def get_prompt(prompt_id: str):
    try:
        logging.info(f"Getting prompt with ID: {prompt_id}")
        prompts_path = os.environ.get("PROMPTS_PATH")
        if not prompts_path:
            #if it is empty it means the user does not have the environment variable set, 
            #so we assume its a local developer and will not populate paths from file share
            prompts_path = "../code/prompts"

        prompt_dir = os.path.join(prompts_path, prompt_id)
        prompt_file = get_latest_file_version(prompt_dir, file_pattern)
        prompt = read_asset_file(prompt_file)[0]
        return prompt
    except Exception as e:
        logging.error(f"Error getting prompt: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

# A GET operation to get all models
@app.get("/models")
def get_models():
    try:
        logging.info("Getting all models")
        return gpt4_models
    except Exception as e:
        logging.error(f"Error getting models: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

# A GET operation to get a file
@app.get("/file")
def get_file(asset_path: str, format:str = "text"):
    try:
        # If asset path begins with ../, replace it with the root path
        if asset_path.startswith("../"):
            asset_path = asset_path.replace("../", f"{ROOT_PATH_INGESTION}/")
        logging.info(f"Getting file: {asset_path}")
        
        if format == "binary":
            return FileResponse(
                asset_path.replace("\\", "/"), 
                # required to ensure the file is displayed in the browser correctly
                content_disposition_type="inline")
        elif format == "text":
            text, status = read_asset_file(asset_path)
            return text
    except Exception as e:
        logging.error(f"Error getting file: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

# Check if the file exists
@app.get("/file_exists")
def check_file_exists(asset_path: str):
    try:
        # If asset path begins with ../, replace it with the root path
        if asset_path.startswith("../"):
            asset_path = asset_path.replace("../", f"{ROOT_PATH_INGESTION}/")
        logging.info(f"Checking if file exists: {asset_path}")
        return os.path.exists(asset_path)
    except Exception as e:
        logging.error(f"Error checking if file exists: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

# a list of objects with role and content as strings
class HistoryMessage(BaseModel):
    role: str
    content: str

class SearchRequest(BaseModel):
    query: str
    top: int
    approx_tag_limit: int
    conversation_history: List[HistoryMessage]    
    user_id: str
    computation_approach: str
    computation_decision: str
    vision_support: bool
    include_master_py: bool
    vector_type: str
    index_name: str
    full_search_output: bool
    count: bool
    token_limit: int
    temperature: float
    verbose: bool

# This middleware will create a new queue for each request and attach it to the request state
# This way, logc calls will log to this queue allowing the response to stream the steps to client before the final result
from utils.logc import log_hook_var

# Generator function to stream the steps to the client
# The generator will yield each step as it is logged and then the final result at the end
# Expected steps are tuples of (kind, content) where kind is either "STEP" or "RESULT", or "END" to signal the end of the stream
async def result_streamer(request_steps_queue: asyncio.Queue):
    while True:
        step = await request_steps_queue.get()
        print(f"\n\nStep: {step}\n\n")
        kind, content = step
        if kind == "END":
            break
        yield json.dumps(step) + "\n" # NEW LINE DELIMITED JSON

# SEARCH endpoint that streams the steps to the client
@app.post("/search-stream")
async def run_search_stream(request: Request):
    try:
        # Parse request body as SearchRequest
        payload = SearchRequest(**await request.json())
        # invoke search function matching the signature using the request object
        logging.info(f"Running search with input: {payload}")
        # Provided by the middleware above
        steps_queue = asyncio.Queue()
        # Search must be run in a separate thread to allow the steps to be streamed to the client
        def run_search_in_thread(input: SearchRequest, request_steps_queue: asyncio.Queue):
            log_hook = lambda message, text=None: request_steps_queue.put_nowait(["STEP", [message, text]])
            token = log_hook_var.set(log_hook)
            try:
                final_answer, references, output_excel, search_results, files = search(
                    query=input.query, 
                    learnings=None, 
                    top=input.top, 
                    approx_tag_limit=input.approx_tag_limit, 
                    conversation_history=input.conversation_history, 
                    user_id=input.user_id, 
                    computation_approach=input.computation_approach, 
                    computation_decision=input.computation_decision, 
                    vision_support=input.vision_support, 
                    include_master_py=input.include_master_py, 
                    vector_directory=os.path.join(ROOT_PATH_INGESTION, input.index_name), 
                    vector_type=input.vector_type, 
                    index_name=input.index_name, 
                    full_search_output=input.full_search_output, 
                    count=input.count, 
                    token_limit=input.token_limit, 
                    temperature=input.temperature, 
                    verbose=input.verbose)
                
                # First put result in the queue, then signal the end of the stream
                request_steps_queue.put_nowait(("RESULT", [final_answer, references, output_excel, search_results, files]))
            except Exception as e:
                logging.error(f"Error running search: {str(e)}", exc_info=True)
                request_steps_queue.put_nowait(("ERROR", str(e), None))
                raise HTTPException(status_code=500, detail=str(e))
            finally:
                # Signal the end of the stream
                # This must be done in a finally block to ensure the stream is closed even if an exception occurs
                request_steps_queue.put_nowait(("END", None)) 
                log_hook_var.reset(token)

         # Create a new thread to run the search function
        search_thread = threading.Thread(target=run_search_in_thread, args=(payload, steps_queue))
        search_thread.start()

        # Return the streaming response
        # NOTE: this must be returned immediately to allow the client to start receiving the stream
        # This is why a separate thread is used to run the search function, headers={"Transfer-Encoding": "identity"}
        return StreamingResponse(result_streamer(steps_queue), media_type="application/x-ndjson")
    except Exception as e:
        logging.error(f"Error running search: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# Dependency to modify log_ui_func_hook based on request data
def modify_log_ui_func_hook(request: Request):
    steps = []
    utils.logc.log_ui_func_hook = lambda message, text=None: steps.append([message, text])
    return steps
# A POST /search that takes a JSON with following structure:
@app.post("/search", description="OBSOLETE, use /search-stream instead")
def run_search(request: SearchRequest, steps = Depends(modify_log_ui_func_hook)):
    try:
        # invoke search function matching the signature using the request object
        logging.info(f"Running search with input: {request}")
        final_answer, references, output_excel, search_results, files = search(
            query=request.query, 
            learnings=None, 
            top=request.top, 
            approx_tag_limit=request.approx_tag_limit, 
            conversation_history=request.conversation_history, 
            user_id=request.user_id, 
            computation_approach=request.computation_approach, 
            computation_decision=request.computation_decision, 
            vision_support=request.vision_support, 
            include_master_py=request.include_master_py, 
            vector_directory=os.path.join(ROOT_PATH_INGESTION, request.index_name), 
            vector_type=request.vector_type, 
            index_name=request.index_name, 
            full_search_output=request.full_search_output, 
            count=request.count, 
            token_limit=request.token_limit, 
            temperature=request.temperature, 
            verbose=request.verbose)
        return final_answer, references, output_excel, search_results, files, steps
    except Exception as e:
        logging.error(f"Error running search: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

# A POST /generate_section that uses the generate_section function
@app.post("/generate_section")
async def generate_new_section(section: Request):
    try:
        logging.info("Generating section")
        return generate_section(await section.json())
    except Exception as e:
        logging.error(f"Error generating section: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

def ensure_download_dictory(index_name):
    """Ensure download directory exists and return the path to it"""
    
    logging.info(f"Current working directory: {os.getcwd()}")
    logging.info(f"Root path ingestion: {ROOT_PATH_INGESTION}")
    ingestion_directory = os.path.join(ROOT_PATH_INGESTION , index_name)
    download_directory = os.path.join(ingestion_directory, 'downloads')
        # log cwd, ingestion directory and download directory
    logging.info(f"Ingestion directory: {ingestion_directory}")
    logging.info(f"Download directory: {download_directory}")
    os.makedirs(download_directory, exist_ok=True)
    return ingestion_directory, download_directory

# A GET operation to get the list of existing files in downaload directory
@app.get("/index/{index_name}/files")
def get_download_files(index_name: str):
    try:
        logging.info("Getting download files")
        
        ingestion_directory, download_directory = ensure_download_dictory(index_name)
        
        files = os.listdir(download_directory)
        existing_file_names = [file for file in files if os.path.isfile(os.path.join(download_directory, file))]
        return existing_file_names
    except Exception as e:
        logging.error(f"Error getting download files: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

# A POST operation to upload files in batches in download directory
@app.post("/index/{index_name}/files")
def upload_files(index_name: str, files: List[UploadFile]):
    try:
        logging.info("Uploading files")
        
        ingestion_directory, download_directory = ensure_download_dictory(index_name)
        
        for file in files:
            file_path = os.path.join(download_directory, file.filename.replace(" ", "_"))
            with open(file_path, "wb") as buffer:
                shutil.copyfileobj(file.file, buffer)
            
        ic.update_cosmos_with_download_files(index_name, download_directory)
            
        return None
    except Exception as e:
        logging.error(f"Error uploading files: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

class IngestionRequest(BaseModel):
    index_name: str
    num_threads: int
    password: str
    delete_existing_output_dir: bool
    processing_mode_pdf: str
    processing_mode_docx: str
    verbose: bool
    
# # A POST /ingest that takes a JSON with the following structure:
# @app.post("/ingest")
# def run_ingestion(request: IngestionRequest):
#     try:
#         logging.info("Running ingestion")
        
#         ingestion_directory, download_directory = ensure_download_dictory(request.index_name)
        
#         dict = request.model_dump()
#         dict['download_directory'] = download_directory
#         dict['ingestion_directory'] = ingestion_directory
#         dict['vision_models'] = gpt4_models
#         dict['models'] = gpt4_models
        
#         return ingest_doc_using_processors(dict)
#     except Exception as e:
#         logging.error(f"Error running ingestion: {str(e)}", exc_info=True)
#         raise HTTPException(status_code=500, detail=str(e))

cogsearch = CogSearchHttpRequest()
# A GET to return the list of cog_search indexes
@app.get("/index")
def get_indexes():
    try:
        logging.info("Getting indexes")
        return cogsearch.get_indexes()
    except Exception as e:
        logging.error(f"Error getting indexes: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

# A GET to get CogSearch index documents, is exists
@app.get("/index/{index_name}/documents")
def get_index_status(index_name: str):
    try:
        logging.info("Getting index status")
        index = CogSearchRestAPI(index_name)
        if index.get_index() is not None:
            documents = index.get_documents()
            return documents
        return None
    except Exception as e:
        logging.error(f"Error getting index status: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

# A GET operation to check indexing status
@app.get("/index/{index_name}/status")
def get_indexing_status(index_name: str):
    try:
        logging.info("Checking indexing status")
        return ic.check_if_indexing_in_progress(index_name)
    except Exception as e:
        logging.error(f"Error checking indexing status: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

# A POST operation to update AmlJob status
@app.post("/index/{index_name}/status")
def update_job_status(index_name: str, request: Request):
    try:
        logging.info("Updating Job status")
        return ic.update_aml_job_status(index_name, request.get("status"))
    except Exception as e:
        logging.error(f"Error updating Job status: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

# A DELETE operation to clear indexing status
@app.delete("/index/{index_name}/status")
def clear_indexing_status(index_name: str):
    try:
        logging.info("Clearing indexing status")
        ic.clear_indexing_in_progress(index_name)
        return None
    except Exception as e:
        logging.error(f"Error clearing indexing status: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

class JobRequest(BaseModel):
    index_name: str
    num_threads: int
    password: str
    delete_existing_output_dir: bool
    processing_mode_pdf: str
    processing_mode_docx: str
    processing_mode_xlsx: str
    chunk_size: int
    chunk_overlap: int
    verbose: bool

# A POST operation to submit an AmlJob
@app.post("/index/{index_name}/aml_job")
def submit_aml_job(index_name: str, request: JobRequest):
    try:
        logging.info(f"Submitting AmlJob from request: {request}")
        ingestion_directory, download_directory = ensure_download_dictory(index_name)
        gpt4_models = get_models()
        
        dict = request.model_dump()
        dict['download_directory'] = download_directory
        dict['ingestion_directory'] = ingestion_directory
        dict['vision_models'] = gpt4_models
        dict['models'] = gpt4_models
        
        # log dict
        logging.info(f"AML job parameters: {dict}")
        
        run_id = aml_job.submit_ingestion_job(dict, script = 'ingest_doc.py', source_directory='./code')
        
        # log run_id
        logging.info(f"Aml job run_id: {run_id}")
        
        ic.update_aml_job_id(index_name, run_id, status = "running")
        return None
    except Exception as e:
        logging.error(f"Error submitting AmlJob: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
    
# POST operation to submit a local ingestion job
@app.post("/index/{index_name}/local_job")
def submit_local_job(index_name: str, request: JobRequest):
    ingestion_directory, download_directory = ensure_download_dictory(index_name)
    
    dict = request.model_dump()
    dict['download_directory'] = download_directory
    dict['ingestion_directory'] = ingestion_directory
    
    # log dict
    logging.info(f"Local job request: {dict}")
    
    process = subprocess.Popen(["python", "./ingest_doc.py", 
                    "--ingestion_params_dict", json.dumps(dict),
                    ])
    
    # log process PID
    logging.info(f"Local job PID: {process.pid}")
    
    return process.pid

@app.get("/job/{job_id}")
def get_job_status(job_id: str):    
    from azure.mgmt.appcontainers import ContainerAppsAPIClient
    from azure.identity import DefaultAzureCredential
    from env_vars import AML_RESOURCE_GROUP, AML_SUBSCRIPTION_ID
    
    try:
        logging.info(f"Getting Job status with ID {job_id}")
        job_name = os.getenv("INGESTION_JOB_NAME")
        job_status = None
        
        if job_name in job_id:
            client = ContainerAppsAPIClient(credential=DefaultAzureCredential(), subscription_id=AML_SUBSCRIPTION_ID)
            job = client.job_execution(AML_RESOURCE_GROUP, job_name, job_id)
            job_status = job.additional_properties['properties']['status'] 
        else:
            job_status = aml_job.check_job_status_using_run_id(job_id)
            
        logging.info(f"Job '{job_id}' status: {job_status}")
        return job_status
    except Exception as e:
        logging.error(f"Error getting Job status ID: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/local_job/{pid}")
def get_local_job_status(pid: str):
    try:
        logging.info(f"Getting local job status from PID {pid}")
        
        # Detect is process is running
        if psutil.pid_exists(int(pid)):
            return "running"
        else:
            return "completed"
    except Exception as e:
        logging.error(f"Error setting AmlJob ID: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

# A GET to fetch processin plan
@app.get("/processing_plan")
def get_processing_plan():
    try:
        logging.info("Getting processing plan")
        proc_plans = read_asset_file("./processing_plan.json")[0]

        return proc_plans
    except Exception as e:
        logging.error(f"Error getting processing plan: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

# A POST to copy the processing plan to the index
@app.post("/index/{index_name}/plan")
def copy_processing_plan_to_index(index_name: str):
    try:
        logging.info("Copying processing plan to index")
        ingestion_directory = os.path.join(ROOT_PATH_INGESTION , index_name) 
        index_processing_plan_path = os.path.join(ingestion_directory, f'{index_name}.processing_plan.txt')
        plans = get_processing_plan()
        os.makedirs(ingestion_directory, exist_ok=True)
        write_to_file(plans, index_processing_plan_path, 'w')
        return None
    except Exception as e:
        logging.error(f"Error copying processing plan to index: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

# A GET to fetch cosmos log
@app.get("/index/{index_name}/log")
def get_cosmos_log(index_name: str):
    try:
        logging.info("Getting cosmos log")
        return cosmos_log.read_document(index_name, index_name)
    except Exception as e:
        logging.error(f"Error getting cosmos log: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# A POST method to execute a container apps job
@app.post("/index/{index_name}/container_apps_job")
def container_apps_job(index_name: str, request: JobRequest):
    from azure.mgmt.appcontainers import ContainerAppsAPIClient
    from azure.mgmt.appcontainers.models import JobExecutionBase, JobExecutionTemplate
    from azure.core.polling import LROPoller
    from azure.identity import DefaultAzureCredential
    from env_vars import AML_RESOURCE_GROUP, AML_SUBSCRIPTION_ID
    
    try:
        logging.info("Executing container apps job")
        
        
        ingestion_directory, download_directory = ensure_download_dictory(index_name)
        gpt4_models = get_models()
        
        dict = request.model_dump()
        dict['download_directory'] = download_directory
        dict['ingestion_directory'] = ingestion_directory
        dict['vision_models'] = gpt4_models
        dict['models'] = gpt4_models
        
        client = ContainerAppsAPIClient(credential=DefaultAzureCredential(), subscription_id=AML_SUBSCRIPTION_ID)
        
        job = client.jobs.get(AML_RESOURCE_GROUP, os.getenv("INGESTION_JOB_NAME"))
        job.template.containers[0].args = ["ingest_doc.py", "--ingestion_params_dict", json.dumps(dict)]
        template = JobExecutionTemplate(containers=job.template.containers)
        poller: LROPoller[JobExecutionBase] = client.jobs.begin_start(
            resource_group_name=AML_RESOURCE_GROUP, 
            job_name= os.getenv("INGESTION_JOB_NAME"),
            template=template)
        
        res: JobExecutionBase = poller.result()
        logging.info(f"Submit job result: {res}")
        run_id = res.name
        
        ic.update_aml_job_id(index_name, run_id, status = "running")
        
        return run_id
    except Exception as e:   
        logging.error(f"Error executing container apps job: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
