import os
from contextlib import contextmanager
from http.server import SimpleHTTPRequestHandler
import socketserver
import logging
import threading
import watchdog.events, watchdog.observers, time
from lxml import etree as ET

from . import static, build

# Get access to logger
log = logging.getLogger('ptxlogger')

@contextmanager
def working_directory(path):
    """
    Temporarily change the current working directory.

    Usage:
    with working_directory(path):
        do_things()   # working in the given path
    do_other_things() # back to original path
    """
    current_directory=os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(current_directory)


def ensure_directory(path):
    """
    If the directory doesn't exist yet, create it.
    """
    try:
        os.makedirs(path)
    except FileExistsError:
        pass


def directory_exists(path):
    """
    Checks if the directory exists.
    """
    return os.path.exists(path)


# Grabs project directory based on presence of `project.ptx`
def project_path(dirpath=os.getcwd()):
    if os.path.isfile(os.path.join(dirpath,'project.ptx')):
        # we're at the project root
        return dirpath
    parentpath = os.path.dirname(dirpath)
    if parentpath == dirpath:
        # cannot ascend higher, no project found
        return None
    else:
        # check parent instead
        return project_path(dirpath=parentpath)

def project_xml(dirpath=os.getcwd()):
    if project_path(dirpath) is None:
        return ET.ElementTree(ET.Element("project"))
    return ET.parse(os.path.join(project_path(dirpath),'project.ptx'))

def target_xml(alias=None,dirpath=os.getcwd()):
    if alias is None:
        return project_xml().find("targets/target")
    xpath = f'targets/target/alias[text()="{alias}"]'
    matches = project_xml().xpath(xpath)
    if len(matches) == 0:
        log.info(f"No targets with alias {alias} found in project manifest file project.ptx.")
        return None
    return project_xml().xpath(xpath)[0].getparent()

def update_from_project_xml(xpath,default=None):
    custom = project_xml().find(xpath)
    if custom is not None:
        return custom.text.strip()
    else:
        return default

#check xml syntax
def xml_syntax_check(xmlfile):
    # parse xml
    try:
        source_xml = ET.parse(xmlfile)
        # we need to call xinclude once for each level of nesting (just to check for errors).  25 levels should be more than sufficient
        for i in range(25):
            source_xml.xinclude()
        log.info('XML syntax appears well formed.')

    # check for file IO error
    except IOError:
        log.error('Invalid File')

    # check for XML syntax errors
    except ET.XMLSyntaxError as err:
        log.error('XML Syntax Error, see error_syntax.log. Quitting...')
        with open('error_syntax.log', 'w') as error_log_file:
            error_log_file.write(str(err.error_log))
        quit()
    except ET.XIncludeError as err:
        log.error(
            'XML Syntax Error with instance of xinclude; see error_syntax.log. Quitting...')
        with open('error_syntax.log', 'w') as error_log_file:
            error_log_file.write(str(err.error_log))
        quit()

def schema_validate(xmlfile):
    #get path to RelaxNG schema file:
    static_dir = os.path.dirname(static.__file__)
    schemarngfile = os.path.join(static_dir, 'schema', 'pretext.rng')

    # Open schemafile for validation:
    relaxng = ET.RelaxNG(file=schemarngfile)

    # Parse xml file:
    source_xml = ET.parse(xmlfile)

    ## just for testing:
    # relaxng.validate(source_xml)
    # log = relaxng.error_log
    # print(log)

    # validate against schema
    try:
        relaxng.assertValid(source_xml)
        log.info('PreTeXt source passed schema validation.')

    except ET.DocumentInvalid as err:
        log.warning('PreTeXt document did not pass schema validation; unexpected output may result. See error_schema.log for hints.  Continuing with build.')
        with open('error_schema.log', 'w') as error_log_file:
            error_log_file.write(str(err.error_log))
        pass



# boilerplate to prevent overzealous caching by preview server, and
# avoid port issues
def serve_forever(directory,binding,port):
    class RequestHandler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=directory, **kwargs)
        """HTTP request handler with no caching"""
        def end_headers(self):
            self.send_my_headers()
            SimpleHTTPRequestHandler.end_headers(self)
        def send_my_headers(self):
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
    class TCPServer(socketserver.TCPServer):
        allow_reuse_address = True
    with TCPServer((binding, port), RequestHandler) as httpd:
        httpd.serve_forever()

class HTMLRebuildHandler(watchdog.events.FileSystemEventHandler):
    def __init__(self,target):
        self.ptxfile = target.find("source").text.strip()
        self.output = target.find("output-dir").text.strip()
        pub_relpath = target.find("publication").text.strip()
        pub_abspath = os.path.abspath(pub_relpath)
        self.stringparams = {"publisher": pub_abspath}
    def on_any_event(self,event):
        log.info("Changes to source found, rebuilding target...")
        build.html(self.ptxfile,self.output,self.stringparams)

def run_server(directory,binding,port,url,watch_target):
    log.info(f"Your build located at `{directory}` may be previewed at")
    log.info(url)
    log.info("Use [Ctrl]+[C] to halt the server.")
    threading.Thread(target=lambda: serve_forever(directory,binding,port),daemon=True).start()
    if watch_target is not None:
        path = os.path.join(project_path(),os.path.dirname(watch_target.find("source").text.strip()))
        log.info(f"Watching for changes in `{os.path.abspath(path)}` ...")
        event_handler = HTMLRebuildHandler(watch_target)
        observer = watchdog.observers.Observer()
        observer.schedule(event_handler, path, recursive=True)
        observer.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("")
        log.info("Closing server...")
        if watch_target is not None: observer.stop()
    if watch_target is not None: observer.join()

# Info on namespaces: http://lxml.de/tutorial.html#namespaces
NSMAP = {
    "xi": "http://www.w3.org/2001/XInclude",
    "xml": "http://www.w3.org/XML/1998/namespace",
}
def nstag(prefix,suffix):
    return "{" + NSMAP[prefix] + "}" + suffix
