import os
import re

import json
import logging

from version import Version, VersionComponent

from .common import cached_property, Implementation, BaseProject, BaseRelease, getjson
from .. import composite_version
from ..archive import Archive
from ..tag import Tag

logger = logging.getLogger(__name__)

class Release(BaseRelease):
	def __init__(self, project, version_info):
		super(Release, self).__init__()

		self.project = project
		self.info = version_info
		self.version = composite_version.try_parse(version_info['version'])
		self.url = version_info['dist']['tarball']
		self.released = project._project_info['time'][self.version.upstream][:10]
	
	@property
	def dependency_names(self):
		def get(source):
			names = map(lambda dep: dep.upstream_id, source)
			names = filter(lambda name: name is not None, names)
			return set(names)

		return get(self.runtime_dependencies).union(get(self.compile_dependencies))
	
	@cached_property
	def release_info(self):
		root = os.listdir(self.archive.local)[0]
		with open(os.path.join(self.archive.local, root, 'package.json')) as json_file:
			return json.load(json_file)
	
	def _enter_archive(self):
		archive = super(Release, self)._enter_archive(extract=None)
		return archive

	def copy(self):
		return type(self)(project=self.project, version_info=self.info)

	def detect_dependencies(self, resolver, nodejs_feed):
		self.runtime_dependencies = []
		self.compile_dependencies = []
		def add_dependency(tagname, name, version_spec, attrs=None, dest=None):
			location = resolver(Npm(name))
			if location is None:
				logging.info("Skipping dependency: %s" % (name,))
				return

			url = location.url
			tag = Tag(tagname, {'interface': url})
			if location.command is not None:
				tag.attr('command', location.command)
			tag.upstream_id = name
			if attrs is not None:
				for k,v in attrs.items():
					tag.attr(k, v)

			version = _parse_version_info(version_spec)
			if version:
				tag.children.append(version)
			if dest is None:
				self.runtime_dependencies.append(tag)
				self.compile_dependencies.append(tag)
			else:
				dest.append(tag)

		package_info = self.release_info
		for (name, version_spec) in package_info.get('dependencies', {}).items():
			add_dependency('requires', name, version_spec)

		# for (name, version_spec) in package_info.get('optionalDependencies', {}).items():
		# 	add_dependency('requires', name, version_spec, {'importance': 'recommended'})

		for (name, version_spec) in package_info.get('peerDependencies', {}).items():
			add_dependency('restricts', name, version_spec)

		for (name, version_spec) in package_info.get('devDependencies', {}).items():
			add_dependency('requires', name, version_spec, dest=self.compile_dependencies)

		engines = package_info.get('engines', {})
		if isinstance(engines, list):
			lst = engines
			engines = {}
			for item in lst:
				# parse ["node>0.6"] into {"node": ">0.6"}
				key, op, spec = [s.strip() for s in re.split('([<>=]+)', item)]
				engines[key] = op+spec

		for (name, version_spec) in engines.items():
			if name == 'node':
				version = _parse_version_info(version_spec)
				tag = Tag('restricts', {'interface':nodejs_feed})
				tag.upstream_id = None
				if version:
					tag.children.append(version)
				for dest in [self.runtime_dependencies, self.compile_dependencies]:
					dest.append(tag)
			else:
				logger.warn("Unknown engine: %s", name)

class Npm(BaseProject):
	upstream_type = 'npm'
	base = 'http://registry.npmjs.org/'

	@property
	def url(self):
		return 'https://npmjs.org/package/' + self.id
	
	@cached_property
	def _project_info(self):
		return getjson(self.base + self.id)

	@cached_property
	def _version_info(self):
		return self._project_info['versions']

	@cached_property
	def summary(self):
		return self._project_info['name'] + " npm package"

	@cached_property
	def description(self):
		return self._project_info['description']

	@cached_property
	def homepage(self):
		return self.url

	@cached_property
	def versions(self):
		return list(self._version_objects.keys())

	@cached_property
	def _version_objects(self):
		res = {}
		for v in self._version_info.values():
			val = Release(self, v)
			if val.version is not None:
				res[val.version] = val
		return res

	def get_release(self, version):
		return self._version_objects[version]
	
	@classmethod
	def parse_uri(cls, uri):
		try:
			match = re.match('(npm:|[^:]+://npmjs.org/package/)(?P<id>[^/]+)', uri)
			return {
				'type': cls.upstream_type,
				'id': match.group('id')
			}
		except StandardError as e:
			logging.debug(e, exc_info=True)
			raise ValueError("can't parse npm project from %s" % (uri,))

def _parse_version_info(spec):
	# https://npmjs.org/doc/json.html#dependencies
	def parse(v):
		v = v.lower()
		v = v.lstrip('v')
		# drop wildcard revisions
		v = re.sub('\.x.*', '', v)

		try:
			return Version.parse(v, coerce=True)
		except StandardError as e:
			logger.debug("Couldn't parse version string: %s" % (v,), exc_info=True)
			logger.warn("Couldn't parse version string: %s" % (v,))
			return None
	
	def inc(v, levels=1):
		if v is None: return None
		return v.increment(levels)
	
	if spec == '' or spec == '*':
		return None
	if spec.startswith('git') or '://' in spec:
		logger.warn("Unparseable version spec: %s - just using first component" % (spec,))
		return _parse_version_info(spec.split('||')[0].strip())
	if '||' in spec:
		logger.warn("Unparseable version spec: %s" % (spec,))
		return None

	# OK, we have a potentially-parseable dependency spec:

	parts = []
	logging.debug("parsing version spec %s" % (spec,))
	for part in filter(lambda x: x.strip(), re.split(r'(<=|>=|[<>=~^])', spec)):
		numbers = part.split()
		for i, number in enumerate(numbers):
			if i > 0:
				parts.append('=')
			parts.append(number)

	logging.debug("got version spec parts: %r" % (parts,))
	restrictions = Tag('version')
	def add(op, ver):
		if ver is not None:
			if op in restrictions:
				# use the most-restrictive version
				best = {
					'before':min,
					'not-before':max
				}[op]
				restrictions[op] = best(ver, restrictions[op])
			else:
				restrictions.attr(op, ver)

	if len(parts) == 1:
		# assume it's an exact version number
		parts.insert(0, '=')

	assert len(parts) % 2 == 0, "Expected an even number of version parts, got: %r" % (parts,)

	while len(parts) > 1:
		op = parts.pop(0)
		number = parts.pop(0)
		ver = parse(number)

		if op == '^' and ver.components[0] == VersionComponent(0):
			# prerelease caret acts like tilde
			op = '~'

		if op == '=':
			add('not-before', ver)
			add('before', inc(ver))
		elif op == '<': add('before', ver)
		elif op == '>': add('not-before', inc(ver))
		elif op == '<=': add('before', inc(ver))
		elif op == '>=': add('not-before', ver)
		elif op == '~':
			add('not-before', ver)

			# make sure it's got exactly 2 components,
			# so that we increment the minor version
			components = ver.components
			while(len(components) < 2): components.append(VersionComponent(0))
			upper_version = Version(components = components[:2]).increment()
			add('before', upper_version)

		elif op == '^':
			add('not-before', ver)
			upper_version = Version(components = ver.components[:1]).increment()
			add('before', upper_version)
		else:
			logging.warn("Unknown version op: %s" % (op,))
		
	logger.debug("restrictions: %r" % (restrictions,))
	return restrictions

