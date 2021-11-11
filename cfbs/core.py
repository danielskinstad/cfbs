#!/usr/bin/env python3
import os
import sys
import logging as log
from collections import OrderedDict
from copy import deepcopy

from cfbs.utils import (
    cfbs_dir,
    cfbs_filename,
    user_error,
    strip_left,
    strip_right,
    pad_right,
    get_json,
    write_json,
    read_json,
    get_or_read_json,
    merge_json,
    read_file,
    find,
    mkdir,
    touch,
    rm,
    cp,
    sh,
    cfengine_dir,
)
from cfbs.internal_file_management import (
    clone_url_repo,
    fetch_archive,
    local_module_name,
    SUPPORTED_ARCHIVES,
)
from cfbs.pretty import pretty

_DEFAULT_INDEX = (
    "https://raw.githubusercontent.com/cfengine/build-index/master/cfbs.json"
)


def _local_module_data_cf_file(module):
    target = os.path.basename(module)
    return {
        "description": "Local policy file added using cfbs command line",
        "tags": ["local"],
        "dependencies": ["autorun"],
        "steps": ["copy %s services/autorun/%s" % (module, target)],
        "added_by": "cfbs add",
    }


def _local_module_data_json_file(module):
    return {
        "description": "Local augments file added using cfbs command line",
        "tags": ["local"],
        "steps": ["json %s def.json" % module],
        "added_by": "cfbs add",
    }


def _local_module_data_subdir(module):
    assert module.startswith("./")
    dst = os.path.join("services", "cfbs", module[2:])
    return {
        "description": "Local subdirectory added using cfbs command line",
        "tags": ["local"],
        "steps": ["directory {} {}".format(module, dst)],
        "added_by": "cfbs add",
    }


def _generate_local_module_object(module):
    assert module.startswith("./")
    assert module.endswith((".cf", ".json", "/"))
    assert os.path.isfile(module) or os.path.isdir(module)

    if os.path.isdir(module):
        return _local_module_data_subdir(module)
    if module.endswith(".cf"):
        return _local_module_data_cf_file(module)
    if module.endswith(".json"):
        return _local_module_data_json_file(module)


class Index:
    """Class representing the cfbs.json containing the index of available modules"""

    def __init__(self, index=_DEFAULT_INDEX):
        self._unexpanded = index
        self._data = None

    def __contains__(self, key):
        return key in self.data["index"]

    def __getitem__(self, key):
        return self.data["index"][key]

    def items(self):
        return self.data["index"].items()

    def get(self, key, default=None):
        if not key in self:
            return default
        return self[key]

    def _cache_path(self) -> str:
        return cfbs_dir("cache.json")

    def _expand_index(self):
        index = self._unexpanded
        if type(index) in (dict, OrderedDict):
            self._data = {"type": "index", "index": index}
            return

        assert type(index) is not Index
        assert type(index) is str

        self._data = get_or_read_json(index)

        if not self._data:
            sys.exit("Could not download or find module index")
        if "index" not in self._data:
            sys.exit("Empty or invalid module index")

    @property
    def data(self) -> dict:
        if not self._data:
            self._expand_index()
        return self._data

    def exists(self, module):
        return os.path.exists(module) or (module in self)

    def get_module_object(self, name):
        if name.startswith("./"):
            return _generate_local_module_object(name)

        module = OrderedDict({"name": name})
        module.update(self[name])
        return module


def _construct_provided_module(name, data, url, url_commit):
    module = OrderedDict()
    module["name"] = name
    if "description" not in data:
        user_error(
            "missing required key 'description' in module definition: %s" % pretty(data)
        )
    module["description"] = data["description"]
    module["url"] = url
    module["commit"] = url_commit
    subdirectory = data.get("subdirectory")
    if subdirectory:
        module["subdirectory"] = subdirectory
    dependencies = data.get("dependencies")
    if dependencies:
        module["dependencies"] = dependencies
    if "steps" not in data:
        user_error(
            "missing required key 'steps' in module definition: %s" % pretty(data)
        )
    module["steps"] = data["steps"]
    module["added_by"] = "cfbs add"
    return module


def _has_autorun_tag(filename):
    assert os.path.isfile(filename)
    content = read_file(filename)

    return (
        "meta:" in content
        and "tags" in content
        and "slist" in content
        and "autorun" in content
    )


class CFBSJson:
    def __init__(
        self,
        path,
        index_argument=None,
        data=None,
        url=None,
        url_commit=None,
    ):
        assert path
        self.path = path
        self.url = url
        self.url_commit = url_commit
        if data:
            self._data = data
        else:
            self._data = read_json(self.path)

        if index_argument:
            self.index = Index(index_argument)
        elif self._data and "index" in self._data:
            self.index = Index(self._data["index"])
        else:
            self.index = Index()

    def get(self, key, default=None):
        if not key in self:
            return default
        return self[key]

    def __getitem__(self, key):
        assert key != "index"
        return self._data[key]

    def __contains__(self, key):
        return key in self._data

    def get_provides(self):
        modules = OrderedDict()
        if "provides" not in self._data:
            user_error(
                "missing required key 'provides' in module definition: %s"
                % pretty(self._data)
            )
        for k, v in self._data["provides"].items():
            module = _construct_provided_module(k, v, self.url, self.url_commit)
            modules[k] = module
        return modules

    def get_module_for_build(self, name, dependent):
        if "provides" in self._data and name in self._data["provides"]:
            module = self._data["provides"][name]
            return _construct_provided_module(name, module, self.url, self.url_commit)
        if name in self.index:
            return self.index.get_module_object(name)
        return None

    def _module_is_in_build(self, module):
        return module["name"] in (m["name"] for m in self["build"])


class CFBSConfig(CFBSJson):
    @staticmethod
    def exists(path="./cfbs.json"):
        return os.path.exists(path)

    @staticmethod
    def validate_added_module(module):
        """Try to help the user with warnings in appropriate cases"""

        name = module["name"]
        if name.startswith("./") and name.endswith(".cf"):
            assert os.path.isfile(name)
            if not _has_autorun_tag(name):
                log.warning("No autorun tag found in policy file: '%s'" % name)
                log.warning("Tag the bundle(s) you want evaluated:")
                log.warning('  meta: "tags" slist => { "autorun" };')
            return
        if name.startswith("./") and name.endswith("/"):
            assert os.path.isdir(name)
            policy_files = list(find(name, extension=".cf"))
            with_autorun = (x for x in policy_files if _has_autorun_tag(x))
            if any(policy_files) and not any(with_autorun):
                log.warning("No bundles tagged with autorun found in: '%s'" % name)
                log.warning("Tag the bundle(s) you want evaluated in .cf policy files:")
                log.warning('  meta: "tags" slist => { "autorun" };')

    def __init__(self, index=None):
        super().__init__(path="./cfbs.json", index_argument=index)

    def save(self):
        data = pretty(self._data) + "\n"
        with open(self.path, "w") as f:
            f.write(data)

    def add_with_dependencies(self, module, remote_config=None, dependent=None):
        if type(module) is str:
            module_str = module
            module = (remote_config or self).get_module_for_build(module, dependent)
        if not module:
            user_error("Module '%s' not found" % module_str)
        assert "name" in module
        assert "steps" in module
        if self._module_is_in_build(module):
            print("Skipping already added module '%s'" % module["name"])
            return
        if "dependencies" in module:
            for dep in module["dependencies"]:
                self.add_with_dependencies(dep, remote_config, module["name"])
        self._data["build"].append(module)
        if dependent:
            print("Added module: %s (Dependency of %s)" % (module["name"], dependent))
        else:
            print("Added module: %s" % module["name"])
        self.validate_added_module(module)

    def _add_using_url(
        self,
        url,
        to_add: list,
        added_by="cfbs add",
        index_path=None,
        checksum=None,
        non_interactive=False,
    ):
        url_commit = None
        if url.endswith(SUPPORTED_ARCHIVES):
            config_path, url_commit = fetch_archive(url, checksum)
        else:
            assert url.startswith(("https://", "git://", "ssh://"))
            config_path, url_commit = clone_url_repo(url)

        remote_config = CFBSJson(path=config_path, url=url, url_commit=url_commit)

        provides = remote_config.get_provides()
        # URL specified in to_add, but no specific modules => let's add all (with a prompt)
        if len(to_add) == 0:
            modules = list(provides.values())
            print("Found %d modules in '%s':" % (len(modules), url))
            for m in modules:
                print("  - " + m["name"])
            if not any(modules):
                user_error("no modules available, nothing to do")
            if not non_interactive:
                answer = input(
                    "Do you want to add all %d of them? [y/N] " % (len(modules))
                )
                if answer.lower() not in ("y", "yes"):
                    return 0
        else:
            missing = [k for k in to_add if k not in provides]
            if missing:
                user_error("Missing modules: " + ", ".join(missing))
            modules = [provides[k] for k in to_add]

        for module in modules:
            self.add_with_dependencies(module, remote_config)

        return 0

    def _add_modules(
        self,
        to_add: list,
        added_by="cfbs add",
        index_path=None,
        checksum=None,
        non_interactive=False,
    ) -> int:
        index = self.index

        # Translate all aliases and remote paths
        translated = []
        for module in to_add:
            if not index.exists(module):
                user_error("Module '%s' does not exist" % module)
            if not module in index and os.path.exists(module):
                translated.append(local_module_name(module))
                continue
            data = index[module]
            if "alias" in data:
                print("%s is an alias for %s" % (module, data["alias"]))
                module = data["alias"]
            translated.append(module)

        to_add = translated

        # added_by can be string, list of strings, or dictionary

        # Convert string -> list:
        if type(added_by) is str:
            added_by = [added_by] * len(to_add)

        # Convert list -> dict:
        if not isinstance(added_by, dict):
            assert len(added_by) == len(to_add)
            added_by = {k: v for k, v in zip(to_add, added_by)}

        # Should have a dict with keys for everything in to_add:
        assert not any((k not in added_by for k in to_add))

        # Print error and exit if there are unknown modules:
        missing = [m for m in to_add if not m.startswith("./") and m not in index]
        if missing:
            user_error("Module(s) could not be found: %s" % ", ".join(missing))

        # If some modules were added as deps previously, mark them as user requested:
        for module in self["build"]:
            if module["name"] in to_add:
                new_added_by = added_by[module["name"]]
                if new_added_by == "cfbs add":
                    module["added_by"] = "cfbs add"

        # Filter modules which are already added:
        added = [m["name"] for m in self["build"]]
        filtered = []
        for module in to_add:
            user_requested = added_by[module] == "cfbs add"
            if module in [*added, *filtered]:
                if user_requested:
                    print("Skipping already added module: %s" % module)
                continue
            filtered.append(module)

        # Find all unmet dependencies:
        dependencies = []
        dependencies_added_by = []
        for module in filtered:
            assert index.exists(module)
            data = index.get_module_object(module)
            assert "alias" not in data
            if "dependencies" in data:
                for dep in data["dependencies"]:
                    if dep not in [*added, *filtered, *dependencies]:
                        dependencies.append(dep)
                        dependencies_added_by.append(module)

        if dependencies:
            self.add_command(dependencies, dependencies_added_by)

        for module in filtered:
            assert index.exists(module)
            data = index.get_module_object(module)
            new_module = {"name": module, **data, "added_by": added_by[module]}
            self["build"].append(new_module)
            if user_requested:
                print("Added module: %s" % module)
            else:
                print(
                    "Added module: %s (Dependency of %s)" % (module, added_by[module])
                )
            added.append(module)

            # TODO: add_command should be refactored to use CFBSConfig.add_with_dependencies()
            self.validate_added_module(new_module)

        return 0

    def add_command(
        self,
        to_add: list,
        added_by="cfbs add",
        index_path=None,
        checksum=None,
        non_interactive=False,
    ) -> int:
        if not to_add:
            user_error("Must specify at least one module to add")

        if to_add[0].endswith(SUPPORTED_ARCHIVES) or to_add[0].startswith(
            ("https://", "git://", "ssh://")
        ):
            return self._add_using_url(
                url=to_add[0],
                to_add=to_add[1:],
                added_by=added_by,
                checksum=checksum,
                non_interactive=non_interactive,
            )

        return self._add_modules(to_add, added_by, checksum, non_interactive)
