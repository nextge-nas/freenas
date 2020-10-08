import asyncio
import collections
import copy
import enum
import os
import shutil
import tempfile
import yaml

from middlewared.schema import accepts, Dict, Str
from middlewared.service import CallError, CRUDService, filterable, filter_list, job, private

from .utils import CHART_NAMESPACE, run, get_storage_class_name


class Resources(enum.Enum):
    CRONJOBS = 'cronjobs'
    DEPLOYMENTS = 'deployments'
    JOBS = 'jobs'
    PODS = 'pods'


class ChartReleaseService(CRUDService):

    class Config:
        namespace = 'chart.release'

    @filterable
    async def query(self, filters=None, options=None):
        if not await self.middleware.call('service.started', 'kubernetes'):
            return []

        k8s_config = await self.middleware.call('kubernetes.config')
        options = options or {}
        extra = copy.deepcopy(options.get('extra', {}))
        get_resources = extra.get('retrieve_resources')
        get_history = extra.get('history')

        if get_resources:
            storage_classes = collections.defaultdict(lambda: None)
            for storage_class in await self.middleware.call('k8s.storage_class.query'):
                storage_classes[storage_class['metadata']['name']] = storage_class

            resources = {r.value: collections.defaultdict(list) for r in Resources}
            for resource, namespace, r_filters, n_func in (
                (
                    Resources.DEPLOYMENTS, 'k8s.deployment', [
                        ['metadata.labels.app\\.kubernetes\\.io/managed-by', '=', 'Helm'],
                    ], lambda r: r['metadata']['labels']['app.kubernetes.io/instance']
                ),
                (
                    Resources.PODS, 'k8s.pod', [['metadata.labels.app\\.kubernetes\\.io/instance', '!=', None]],
                    lambda r: r['metadata']['labels']['app.kubernetes.io/instance']
                ),
                (
                    Resources.JOBS, 'k8s.job', [['metadata.labels.app\\.kubernetes\\.io/instance', '!=', None]],
                    lambda r: r['metadata']['labels']['app.kubernetes.io/instance']
                ),
                (
                    Resources.CRONJOBS, 'k8s.cronjob', [['metadata.labels.app\\.kubernetes\\.io/instance', '!=', None]],
                    lambda r: r['metadata']['labels']['app.kubernetes.io/instance']
                ),
            ):
                r_filters += [['metadata.namespace', '=', CHART_NAMESPACE]]
                for r_data in await self.middleware.call(f'{namespace}.query', r_filters):
                    resources[resource.value][n_func(r_data)].append(r_data)

        release_secrets = await self.middleware.call('chart.release.releases_secrets', extra)
        releases = []
        for name, release in release_secrets.items():
            config = {}
            release_data = release['releases'].pop(0)
            cur_version = release_data['chart_metadata']['version']

            for rel_data in filter(
                lambda r: r['chart_metadata']['version'] == cur_version,
                reversed(release['releases'])
            ):
                config.update(rel_data['config'])

            release_secret = release['secrets'][0]
            release_data.update({
                'catalog': release_secret['metadata']['labels'].get(
                    'catalog', await self.middleware.call('catalog.official_catalog_label')
                ),
                'catalog_train': release_secret['metadata']['labels'].get('catalog_train', 'test'),
                'path': os.path.join('/mnt', k8s_config['dataset'], 'releases', name),
                'dataset': os.path.join(k8s_config['dataset'], 'releases', name),
                'config': config,
            })
            if get_resources:
                release_data['resources'] = {
                    'storage_class': storage_classes[get_storage_class_name(name)],
                    **{r.value: resources[r.value][name] for r in Resources},
                }
            if get_history:
                release_data['history'] = release['history']

            releases.append(release_data)

        return filter_list(releases, filters, options)

    @private
    async def normalise_and_validate_values(self, item_details, values, update, release_name):
        dict_obj = await self.middleware.call('chart.release.validate_values', item_details, values, update)
        return await self.middleware.call(
            'chart.release.get_normalised_values', dict_obj, values, update, {
                'release_name': release_name,
            }
        )

    @accepts(
        Dict(
            'chart_release_create',
            Dict('values', additional_attrs=True),
            Str('catalog', required=True),
            Str('item', required=True),
            Str('release_name', required=True),
            Str('train', default='charts'),
            Str('version', required=True),
        )
    )
    async def do_create(self, data):
        await self.middleware.call('kubernetes.validate_k8s_setup')
        if await self.middleware.call('chart.release.query', [['id', '=', data['release_name']]]):
            raise CallError(f'Chart release with {data["release_name"]} already exists.')

        catalog = await self.middleware.call(
            'catalog.query', [['id', '=', data['catalog']]], {'get': True, 'extra': {'item_details': True}}
        )
        if data['train'] not in catalog['trains']:
            raise CallError(f'Unable to locate "{data["train"]}" catalog train.')
        if data['item'] not in catalog['trains'][data['train']]:
            raise CallError(f'Unable to locate "{data["item"]}" catalog item.')
        if data['version'] not in catalog['trains'][data['train']][data['item']]['versions']:
            raise CallError(f'Unable to locate "{data["version"]}" catalog item version.')

        item_details = catalog['trains'][data['train']][data['item']]['versions'][data['version']]
        # The idea is to validate the values provided first and if it passes our validation test, we
        # can move forward with setting up the datasets and installing the catalog item
        default_values = item_details['values']
        new_values = copy.deepcopy(default_values)
        new_values.update(data['values'])
        new_values = await self.normalise_and_validate_values(item_details, new_values, False, data['release_name'])

        # Now that we have completed validation for the item in question wrt values provided,
        # we will now perform the following steps
        # 1) Create release datasets
        # 2) Copy chart version into release/charts dataset
        # 3) Install the helm chart
        # 4) Create storage class
        k8s_config = await self.middleware.call('kubernetes.config')
        release_ds = os.path.join(k8s_config['dataset'], 'releases', data['release_name'])
        storage_class_name = await get_storage_class_name(data['release_name'])
        try:
            for dataset in await self.release_datasets(release_ds):
                if not await self.middleware.call('pool.dataset.query', [['id', '=', dataset]]):
                    await self.middleware.call('pool.dataset.create', {'name': dataset, 'type': 'FILESYSTEM'})

            chart_path = os.path.join('/mnt', release_ds, 'charts', data['version'])
            await self.middleware.run_in_thread(lambda: shutil.copytree(item_details['location'], chart_path))

            with tempfile.NamedTemporaryFile(mode='w+') as f:
                f.write(yaml.dump(new_values))
                f.flush()
                # We will install the chart now and force the installation in an ix based namespace
                # https://github.com/helm/helm/issues/5465#issuecomment-473942223
                cp = await run(
                    [
                        'helm', 'install', data['release_name'], chart_path, '-n',
                        CHART_NAMESPACE, '--create-namespace', '-f', f.name,
                    ],
                    check=False,
                )
            if cp.returncode:
                raise CallError(f'Failed to install catalog item: {cp.stderr}')

            storage_class = await self.middleware.call('k8s.storage_class.retrieve_storage_class_manifest')
            storage_class['metadata']['name'] = storage_class_name
            storage_class['parameters']['poolname'] = os.path.join(release_ds, 'volumes')
            if await self.middleware.call('k8s.storage_class.query', [['metadata.name', '=', storage_class_name]]):
                # It should not exist already, but even if it does, that's not fatal
                await self.middleware.call('k8s.storage_class.update', storage_class_name, storage_class)
            else:
                await self.middleware.call('k8s.storage_class.create', storage_class)

            # TODO: Let's see doing this with k8s possibly making it more robust
            await self.middleware.call(
                'chart.release.update_unlabelled_secrets_for_release',
                data['release_name'], data['catalog'], data['train'],
            )
        except Exception:
            # Do a rollback here
            # Let's uninstall the release as well if it did get installed ( it is possible this might have happened )
            if await self.query([['id', '=', data['release_name']]]):
                delete_job = await self.middleware.call('chart.release.delete', data['release_name'])
                await delete_job.wait()
                if delete_job.error:
                    self.logger.error('Failed to uninstall helm chart release: %s', delete_job.error)
            else:
                await self.remove_storage_class_and_dataset(data['release_name'])

            raise

    @accepts(
        Str('chart_release'),
        Dict(
            'chart_release_update',
            Dict('values', additional_attrs=True),
        )
    )
    async def do_update(self, chart_release, data):
        release = await self.get_instance(chart_release)
        chart_path = os.path.join(release['path'], 'charts', release['chart_metadata']['version'])
        if not os.path.exists(chart_path):
            raise CallError(
                f'Unable to locate {chart_path!r} chart version for updating {chart_release!r} chart release'
            )

        version_details = await self.middleware.call('catalog.item_version_details', chart_path)
        config = release['config']
        config.update(data['values'])
        config = await self.normalise_and_validate_values(version_details, config, True, chart_release)

        with tempfile.NamedTemporaryFile(mode='w+') as f:
            f.write(yaml.dump(config))
            f.flush()

            cp = await run(
                ['helm', 'upgrade', chart_release, chart_path, '-n', CHART_NAMESPACE, '-f', f.name], check=False
            )
            if cp.returncode:
                raise CallError(f'Failed to update chart release: {cp.stderr.decode()}')

    @accepts(Str('release_name'))
    @job(lock=lambda args: f'chart_release_delete_{args[0]}')
    async def do_delete(self, job, release_name):
        # For delete we will uninstall the release first and then remove the associated datasets
        await self.middleware.call('kubernetes.validate_k8s_setup')
        release = await self.query([['id', '=', release_name]], {'extra': {'retrieve_resources': True}, 'get': True})
        pods = release['resources'][Resources.PODS.value]

        cp = await run(['helm', 'uninstall', release_name, '-n', CHART_NAMESPACE], check=False)
        if cp.returncode:
            raise CallError(f'Unable to uninstall "{release_name}" chart release: {cp.stderr}')

        job.set_progress(50, f'Uninstalled {release_name}')
        # wait for release to uninstall properly, helm right now does not support a flag for this but
        # a feature request is open in the community https://github.com/helm/helm/issues/2378
        while await self.middleware.call(
            'k8s.pod.query', [
                ['metadata.name', 'in', [p['metadata']['name'] for p in pods]],
                ['metadata.namespace', '=', CHART_NAMESPACE],
            ],
        ):
            job.set_progress(75, f'Waiting for {release_name!r} pods to terminate')
            await asyncio.sleep(5)

        await self.remove_storage_class_and_dataset(release_name, job)

        job.set_progress(100, f'{release_name!r} chart release deleted')

    @private
    async def remove_storage_class_and_dataset(self, release_name, job=None):
        storage_class_name = await get_storage_class_name(release_name)
        if await self.middleware.call('k8s.storage_class.query', [['metadata.name', '=', storage_class_name]]):
            if job:
                job.set_progress(85, f'Removing {release_name!r} storage class')
            try:
                await self.middleware.call('k8s.storage_class.delete', storage_class_name)
            except Exception as e:
                self.logger.error('Failed to remove %r storage class: %s', storage_class_name, e)

        k8s_config = await self.middleware.call('kubernetes.config')
        release_ds = os.path.join(k8s_config['dataset'], 'releases', release_name)
        if await self.middleware.call('pool.dataset.query', [['id', '=', release_ds]]):
            if job:
                job.set_progress(95, f'Removing {release_ds!r} dataset')
            await self.middleware.call('zfs.dataset.delete', release_ds, {'recursive': True, 'force': True})

    @private
    async def release_datasets(self, release_dataset):
        return [release_dataset] + [os.path.join(release_dataset, k) for k in ('charts', 'volumes')]
