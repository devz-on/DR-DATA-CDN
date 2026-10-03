"""Publish manifest-listed native game assets to free GitHub Releases.

Runs on a public GitHub Actions Linux runner. Credentials stay in its temporary
GITHUB_TOKEN; the input archive URLs expire and are never written to the repo.
"""
import concurrent.futures
import hashlib
import http.client
import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

REPO = os.environ['GITHUB_REPOSITORY']
TOKEN = os.environ['GITHUB_TOKEN']
BASE = 'https://api.github.com/repos/'+REPO
HEADERS = {'Authorization': 'Bearer '+TOKEN, 'Accept': 'application/vnd.github+json',
           'User-Agent': 'DevzON-Native-Data', 'X-GitHub-Api-Version': '2022-11-28'}


def api(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(BASE+path, data=data, method=method,
        headers={**HEADERS, **({'Content-Type':'application/json'} if data else {})})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response) if response.status != 204 else None


def download(url, destination, expected=None):
    if urllib.parse.urlsplit(url).hostname not in ('github.com', 'release-assets.githubusercontent.com',
                                                  'objects.githubusercontent.com'):
        raise ValueError('Unexpected archive host')
    sha = hashlib.sha256()
    with urllib.request.urlopen(url, timeout=300) as response, destination.open('wb') as output:
        while chunk := response.read(1024*1024):
            output.write(chunk); sha.update(chunk)
    if expected and sha.hexdigest() != expected:
        raise ValueError('Archive SHA256 mismatch')


def upload(release, item, source):
    target = urllib.parse.urlsplit(release['upload_url'].split('{',1)[0])
    size = source.stat().st_size
    for attempt in range(10):
        connection = http.client.HTTPSConnection(target.hostname, timeout=300)
        try:
            connection.putrequest('POST', target.path+'?'+urllib.parse.urlencode({'name':item['asset']}))
            for key,value in HEADERS.items(): connection.putheader(key,value)
            connection.putheader('Content-Type','application/octet-stream')
            connection.putheader('Content-Length',str(size)); connection.endheaders()
            with source.open('rb') as stream:
                while chunk := stream.read(1024*1024): connection.send(chunk)
            response = connection.getresponse(); payload = response.read()
            if response.status == 201:
                asset = json.loads(payload)
                if (asset['size'] != size or asset['name'] != item['asset']
                        or asset.get('digest') not in (None, 'sha256:'+item['sha256'])):
                    raise ValueError('Uploaded asset checksum or metadata differs')
                return
            if response.status in (403,429,502,503):
                delay = min(900,max(int(response.getheader('Retry-After') or 60),60*(attempt+1)))
                print('Upload throttled; respecting server retry delay of',delay,'seconds',flush=True)
                time.sleep(delay); continue
            if response.status == 422:
                # A prior response may have been lost after a successful upload.
                for asset in list_assets(release):
                    if (asset['name'] == item['asset'] and asset['size'] == size
                            and asset.get('digest') == 'sha256:'+item['sha256']): return
            raise RuntimeError('Release upload HTTP '+str(response.status))
        finally:
            connection.close()
    raise RuntimeError('Upload retry limit reached; rerun resumes verified assets')


def list_assets(release):
    result=[]
    for page in range(1,12):
        batch=api('GET',f'/releases/{release["id"]}/assets?per_page=100&page={page}')
        result.extend(batch)
        if len(batch)<100: break
    return result


def main():
    plan=json.loads(Path('cdn-plan.json').read_text())
    archives=json.loads(os.environ['ARCHIVE_URLS'])
    for item in archives: print('::add-mask::'+item['url'])
    cache=Path('/tmp/devzon-resources'); cache.mkdir(exist_ok=True)
    wanted={item['name']:item for item in plan['files']}
    for index,item in enumerate(archives):
        destination=cache/('source-'+str(index)+'.zip')
        download(item['url'],destination,item['sha256'])
        with zipfile.ZipFile(destination) as archive:
            for member in archive.infolist():
                name=member.filename.removeprefix('PUB/Resource/').removeprefix('Resource/')
                if name not in wanted: continue
                target=cache/name; target.parent.mkdir(parents=True,exist_ok=True)
                target.write_bytes(archive.read(member))
        destination.unlink()
    for item in plan['files']:
        source=cache/item['name']
        if source.stat().st_size != item['size'] or hashlib.sha256(source.read_bytes()).hexdigest()!=item['sha256']:
            raise ValueError('Native resource mismatch: '+item['name'])
    print('Verified',len(wanted),'files;',plan['resource_bytes'],'bytes.',flush=True)
    for tag in sorted({item['tag'] for item in plan['files']}):
        try: release=api('GET','/releases/tags/'+tag)
        except urllib.error.HTTPError as error:
            if error.code!=404: raise
            release=api('POST','/releases',{'tag_name':tag,'name':tag,'target_commitish':'main',
                'draft':True,'prerelease':True,'body':'Verified native patcher resource files. Unofficial DevzON fan project.'})
        existing={asset['name']:asset for asset in list_assets(release)}
        files=[item for item in plan['files'] if item['tag']==tag]
        pending=[]
        for item in files:
            asset=existing.get(item['asset'])
            if asset and asset['state']=='uploaded' and asset['size']==item['size'] and asset.get('digest')=='sha256:'+item['sha256']:
                continue
            if asset: api('DELETE',f'/releases/assets/{asset["id"]}')
            pending.append(item)
        complete=len(files)-len(pending)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(upload,release,item,cache/item['name']) for item in pending]
            for future in concurrent.futures.as_completed(futures):
                future.result(); complete+=1
                if complete%25==0 or complete==len(files):
                    print(tag,complete,'/',len(files),'uploaded',flush=True)
        assets={asset['name']:asset for asset in list_assets(release)}
        if any(assets.get(item['asset'],{}).get('digest')!='sha256:'+item['sha256'] for item in files):
            raise ValueError('Release verification failed')
        api('PATCH',f'/releases/{release["id"]}',{'draft':False})
        print('Published',tag,flush=True)
    print('CDN_READY',plan['manifest_sha256'],flush=True)


if __name__=='__main__': main()
