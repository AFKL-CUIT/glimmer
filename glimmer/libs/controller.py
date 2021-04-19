from glob import glob
from os import path
from sys import path as sys_path
from pathlib import Path
from queue import Queue
from time import strftime
from concurrent.futures import ThreadPoolExecutor, as_completed

from click import UsageError
from libs.core.parser import parse_path

from utils import cprint, header
from libs.request import patch_request
from libs.logger import init_logger, logger
from libs.core.loader import load_module
from libs.core.config import CONFIG, PLUGINS, POCS, ConfigHandler
from libs.core.exceptions import ModuleLoadExceptions


def _verify_poc(module):
    """Verify poc module"""
    if not hasattr(module, "Poc"):
        raise ModuleLoadExceptions.VerifyError("non-existent class: Poc")
    module_class_poc = getattr(module, "Poc")
    if not isinstance(module_class_poc, object):
        raise ModuleLoadExceptions.VerifyError("error attribute type: Poc")
    if not hasattr(module_class_poc, "check"):
        raise ModuleLoadExceptions.VerifyError(
            "non-existent method: Poc.check")


def _set_config(root_path, verbose, very_verbose):
    # set root_path
    logger.info("_set_config: set root_path")
    sys_path.insert(0, root_path)
    CONFIG.base.root_path = Path(root_path)
    # set verbose flag
    logger.info("_set_config: set verbose flag")
    CONFIG.option.verbose = verbose
    CONFIG.option.very_verbose = very_verbose
    ...


def _load_poc(poc_path, fullname=None, msgType="", verify_func=None):
    try:
        module = load_module(poc_path, fullname, verify_func)
    except ModuleLoadExceptions.Base as e:
        msg = header(msgType, "-", "load poc %s error: " %
                     fullname + str(e) + "\n")
        return None, msg
    else:
        msg = header(msgType, "+", "load poc %s\n" % fullname)
        return module, msg


def _work(tasks_queue):
    if not tasks_queue.empty():
        target, poc = tasks_queue.get()
        try:
            res = poc.check(target)
            return target, poc, res
        except Exception as err:
            res = {"url": "",
                   "status": -1,
                   "msg": "work error: " + str(err),
                   "extra": {}
                   }
        return target, poc, res
    return None, None, None


def _run(threads, tasks_queue, results, timeout, output_handlers):
    with ThreadPoolExecutor(threads) as pool:
        # create tasks
        futures = [pool.submit(_work, tasks_queue) for _ in range(threads)]
        # get result as completed
        for future in as_completed(futures, timeout):
            target, poc, poc_result = future.result()
            if target and poc and poc_result:
                status = poc_result.get("status", -1)
                if status == 0:
                    logger_func = logger.info
                else:
                    logger_func = logger.error
                logger_func("_run: done poc: %s" % poc.name)

                results[target][poc.name] = poc_result
                # handle result
                _output(output_handlers, poc, poc_result)
    return results


def _output(output_handlers, poc, poc_result):
    for handler in output_handlers:
        handler(poc, poc_result)


def load_config(config_path):
    if config_path and path.isfile(config_path):
        logger.info("load_config: load configuration from file")
        config = ConfigHandler(config_path)
        CONFIG.base.config = config
        request_config = config.get("request")
        CONFIG.base.request = request_config
    else:
        logger.warning("load_config: config_path [%s] not found" % config_path)


def load_targets(urls, files):
    if not any((urls, files)):
        raise UsageError("option url/file is required")
    targets = []
    if urls:  # load from urls
        targets.extend(urls)
    if files:  # load from files
        for file in files:
            if not path.isfile(file):
                raise UsageError("non-existent path: %s" % file)
            with open(file, "r") as f:
                targets.extend([line.strip()
                               for line in f.readlines() if line])
    targets = [parse_path(target, ("parser.url",)) for target in targets]
    CONFIG.base.targets = targets
    for target in targets:
        logger.info(header("Load target", "*", target), extra={"markup": True})


def load_pocs(pocs_path="", pocs=[]):
    pocs_path = Path(CONFIG.base.root_path /
                     "pocs") if not pocs_path else Path(pocs_path)
    debug_msg = ""
    msg = ""
    instances = POCS.instances
    count_dict = {}
    if not pocs:
        pocs = [poc for poc in glob(str(pocs_path / "**" / "*.py"))]
    for poc in pocs:
        poc_path = poc
        fname = poc
        logger_func = logger.info
        if "://" not in poc:  # choose a poc from poc dir
            poc_path = str(pocs_path / poc)
            if not poc_path.endswith(".py"):
                poc_path += ".py"
            if not path.isfile(poc_path):
                raise ModuleLoadExceptions.FileNotFound(
                    "%s not found" % poc_path)
            poc_type_dir = path.basename(path.dirname(poc))
            fname, _ = path.splitext(path.basename(poc))
            fname = "%s/%s" % (poc_type_dir, fname)
            poc_path = "file://" + poc_path
        else:
            poc_type_dir = "_" + poc[:poc.index("://")]

        module, load_msg = _load_poc(
            poc_path, fname, "Load %s poc" % poc_type_dir, _verify_poc)
        if module:
            if poc_type_dir not in count_dict:
                count_dict[poc_type_dir] = 0
            count_dict[poc_type_dir] += 1
            instances[fname] = module.Poc()
        else:
            msg += load_msg
            logger_func = logger.error
        debug_msg += load_msg

        logger_func(load_msg, extra={"markup": True})

    msg = "\n".join(header("Load %s poc" % k, "+",
                    "Loaded [%d] pocs" % v) for k, v in count_dict.items()) + msg + "\n"
    POCS.messages = msg
    POCS.debug_messages = debug_msg

    if CONFIG.option.get("very_verbose", False):
        cprint(debug_msg)
    elif CONFIG.option.get("verbose", False):
        cprint(msg)


def load_plugins(plugins_path):
    from importlib import import_module
    plugins_path = Path(CONFIG.base.root_path /
                        "plugins") if not plugins_path else Path(plugins_path)
    msg = ""
    debug_msg = ""
    count_dict = {}

    for f in glob(str(plugins_path / "**" / "*.py")):
        filename = path.basename(f)
        fname, _ = path.splitext(filename)
        plugin_type_dir = path.basename(path.dirname(f))
        try:
            import_module("plugins.%s.%s" % (plugin_type_dir, fname))
            temp_msg = header("Load plugin", "+", "load plugin %s.%s \n" %
                              (plugin_type_dir, fname))
            debug_msg += temp_msg
            if plugin_type_dir not in count_dict:
                count_dict[plugin_type_dir] = 0
            count_dict[plugin_type_dir] += 1

            logger.info(temp_msg, extra={"markup": True})
        except ImportError as e:
            temp_msg = header("Load plugin", "-", "load plugin %s.%s error: " %
                              (plugin_type_dir, fname) + str(e) + "\n")
            debug_msg += temp_msg
            msg += temp_msg

            logger.error(temp_msg, extra={"markup": True})

    msg = "\n".join(header("Load %s plugin" % k, "+",
                    "Loaded [%d] plugins" % v) for k, v in count_dict.items()) + msg + "\n"
    PLUGINS.messages = msg
    PLUGINS.debug_messages = debug_msg
    if CONFIG.option.get("very_verbose", False):
        cprint(debug_msg)
    elif CONFIG.option.get("verbose", False):
        cprint(msg)


def enable_plugins(outs, *args):
    enable_plugins_name = []
    outs = ["output/%s" % out for out in outs]
    enable_plugins_name.extend(outs)
    if args:
        for arg in args:
            if isinstance(arg, (tuple, list)):
                enable_plugins_name.extend(arg)
            else:
                enable_plugins_name.append(arg)
    PLUGINS.enable_plugins_name = enable_plugins_name


def init_plugins():
    logger.info("init_plugins: construct plugins")
    for plg_name, plg in PLUGINS.instances.items():
        if plg_name in PLUGINS.enable_plugins_name:
            plg.construct()


def init_output_plugins(outs):
    output_handlers = [instance.handle for plg_name, instance in PLUGINS.output.items() if plg_name in outs]
    PLUGINS.output_handlers = output_handlers


def end_plugins():
    logger.info("end_plugins: destruct plugins")
    for plg_name, plg in PLUGINS.instances.items():
        if plg_name in PLUGINS.enable_plugins_name:
            plg.destruct()


def init(root_path, verbose, very_verbose, debug):
    init_logger(debug)

    _set_config(root_path, verbose, very_verbose)

    patch_request()

    ...


def start(threads, timeout):
    logger.info("start: start program")
    targets = CONFIG.base.targets
    tasks_queue = Queue()
    results = {}
    pocs = [poc for poc in POCS.instances.values()]
    for target in targets:
        results[target] = {}
        for poc in pocs:
            tasks_queue.put((target, poc))
    return _run(threads, tasks_queue, results, timeout, PLUGINS.output_handlers)


def end():
    cprint("\n" + header("End", "*", "shutting down at %s" % strftime("%X")))
    ...
