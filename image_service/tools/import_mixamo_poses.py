"""Convert local Mixamo FBX clips into distinct static VNCCS poses, without network access."""

import argparse
import asyncio
import base64
from collections import Counter
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

import numpy as np

try:
    from playwright.async_api import async_playwright
except ImportError:
    async_playwright = None

from .describe_poses import library_file
from ..paths import POSE_ASSET_ROOT


JOINTS = ('pelvis', 'spine_03', 'neck_01', 'head',
          'upperarm_l', 'lowerarm_l', 'hand_l', 'upperarm_r', 'lowerarm_r', 'hand_r',
          'thigh_l', 'calf_l', 'foot_l', 'ball_l', 'thigh_r', 'calf_r', 'foot_r', 'ball_r')


def pose_features(frames):
    features = np.asarray([[frame['joints'][name] for name in JOINTS] for frame in frames], dtype=np.float64)
    if features.shape != (len(frames), len(JOINTS), 3) or not np.isfinite(features).all():
        raise ValueError('转换后的关节坐标无效')
    for frame in frames:
        angles = np.asarray(list(frame['pose']['bones'].values()), dtype=np.float64)
        if angles.size and (angles.ndim != 2 or angles.shape[1] != 3 or not np.isfinite(angles).all()):
            raise ValueError('转换后的骨骼旋转无效')
    return features


def select_frames(features, accepted, count, minimum):
    # Distances are RMS joint displacements, measured in standing body heights.
    if len(accepted):
        distances = np.sqrt(np.mean(np.sum((features[:, None] - accepted[None]) ** 2, axis=-1), axis=-1)).min(axis=1)
    else:
        distances = np.sqrt(np.mean(np.sum((features - features.mean(axis=0)) ** 2, axis=-1), axis=-1))
    selected = []
    for _ in range(count):
        index = int(np.argmax(distances))
        if (selected or len(accepted)) and distances[index] < minimum:
            break
        selected.append(index)
        next_distances = np.sqrt(np.mean(np.sum((features - features[index]) ** 2, axis=-1), axis=-1))
        distances = np.minimum(distances, next_distances) if len(accepted) or len(selected) > 1 else next_distances
        distances[selected] = -1
    return sorted(selected)


def describe_pose(item, joints):
    zh, en = [], []
    lateral = np.asarray(joints['upperarm_l']) - joints['upperarm_r']
    lateral /= np.linalg.norm(lateral)
    for side, side_zh, side_en in [('l', '左', 'Left'), ('r', '右', 'Right')]:
        hand, shoulder, hip = (joints[f'{name}_{side}'] for name in ('hand', 'upperarm', 'thigh'))
        if hand[1] > joints['head'][1]:
            zh.append(f'{side_zh}手高举过头'); en.append(f'{side_en} hand raised above the head')
        elif hand[1] > shoulder[1] - 0.04:
            zh.append(f'{side_zh}手抬至肩部附近或更高'); en.append(f'{side_en} hand at shoulder height or higher')
        elif hand[1] > hip[1] + 0.08:
            zh.append(f'{side_zh}手位于腰腹至胸部高度'); en.append(f'{side_en} hand between waist and chest height')
        else:
            zh.append(f'{side_zh}手位于髋部附近或更低'); en.append(f'{side_en} hand at hip height or lower')
        shoulder, elbow, wrist = (np.asarray(joints[f'{name}_{side}']) for name in ('upperarm', 'lowerarm', 'hand'))
        upper, lower = elbow - shoulder, wrist - elbow
        bend = np.degrees(np.arccos(np.clip(np.dot(upper, lower) / (np.linalg.norm(upper) * np.linalg.norm(lower)), -1, 1)))
        if bend > 60:
            zh.append(f'{side_zh}臂屈肘'); en.append(f'{side_en} elbow flexed')
        elif bend < 25:
            zh.append(f'{side_zh}臂伸直'); en.append(f'{side_en} arm extended')
        if abs(np.dot(wrist - shoulder, lateral)) > 0.18:
            zh.append(f'{side_zh}手向侧方伸出'); en.append(f'{side_en} hand reaching sideways')
        thigh, knee, foot = (np.asarray(joints[f'{name}_{side}']) for name in ('thigh', 'calf', 'foot'))
        upper, lower = knee - thigh, foot - knee
        bend = np.degrees(np.arccos(np.clip(np.dot(upper, lower) / (np.linalg.norm(upper) * np.linalg.norm(lower)), -1, 1)))
        if bend > 35:
            zh.append(f'{side_zh}膝弯曲'); en.append(f'{side_en} knee bent')
    if abs(joints['foot_l'][1] - joints['foot_r'][1]) > 0.12:
        side = '左' if joints['foot_l'][1] > joints['foot_r'][1] else '右'
        zh.append(f'{side}脚抬高'); en.append(('Left' if side == '左' else 'Right') + ' foot raised')
    torso = np.asarray(joints['neck_01']) - joints['pelvis']
    tilt = np.degrees(np.arccos(np.clip(torso[1] / np.linalg.norm(torso), -1, 1)))
    if tilt > 25:
        zh.append('躯干明显倾斜'); en.append('Torso tilted away from upright')
    if np.linalg.norm(np.asarray(joints['foot_l']) - joints['foot_r']) > 0.3:
        zh.append('双脚分开'); en.append('Feet spread apart')
    return {'zh': f"{item['category']}，{item['label_zh']}动作中的定格姿势。" + '，'.join(zh) + '。',
            'en': f"A static pose sampled from {item['description']}. " + '; '.join(en) + '.'}


async def convert(args):
    if async_playwright is None:
        raise ValueError('请先安装 requirements-pose.txt 中的浏览器依赖')
    manifest = json.loads(args.manifest.read_text())
    items = manifest['items'][:args.limit_clips] if args.limit_clips else manifest['items']
    dependencies = json.loads((args.dependencies / 'manifest.json').read_text())
    cached_modules = {}
    for url, entry in dependencies.items():
        data = library_file(args.dependencies.resolve(), entry['file']).read_bytes()
        if hashlib.sha256(data).hexdigest() != entry['sha256']:
            raise ValueError(f'解析依赖校验失败：{entry["file"]}')
        cached_modules[url] = data
    web = POSE_ASSET_ROOT
    output = args.output_dir.resolve()
    library = output / 'library'
    cache = output / 'converted'
    cache.mkdir(parents=True, exist_ok=True)
    accepted = np.empty((0, len(JOINTS), 3), dtype=np.float64)
    report = {'dataset': manifest['dataset'], 'revision': manifest['revision'], 'license': manifest['license'],
              'manifest_sha256': hashlib.sha256(args.manifest.read_bytes()).hexdigest(),
              'minimum_distance': args.min_distance, 'per_clip': args.per_clip, 'limit_clips': args.limit_clips,
              'description_method': 'source action label plus measured joint heights and knee bends',
              'items': []}
    report_path = output / 'report.json'
    if report_path.exists():
        previous = json.loads(report_path.read_text())
        if any(previous[key] != report[key] for key in ('manifest_sha256', 'minimum_distance', 'per_clip', 'limit_clips')):
            raise ValueError('输入或去重设置已变化，请使用新的输出目录')
    async with async_playwright() as driver:
        browser = await driver.chromium.launch(executable_path=str(args.browser), headless=True,
            args=['--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--disable-gpu-shader-disk-cache'])
        page = await browser.new_page()

        async def route(request):
            url = request.request.url
            if url in cached_modules:
                await request.fulfill(content_type='text/javascript', body=cached_modules[url])
                return
            parsed = urlsplit(url)
            if parsed.scheme != 'http' or parsed.netloc != '127.0.0.1':
                await request.abort()
            elif parsed.path == '/':
                await request.fulfill(content_type='text/html', path=str(Path(__file__).with_suffix('.html')))
            elif parsed.path.startswith('/fbx/'):
                index = int(parsed.path.removeprefix('/fbx/'))
                if not 0 <= index < len(items):
                    await request.abort()
                    return
                path = library_file(args.source_dir.resolve(), items[index]['file'])
                await request.fulfill(path=str(path))
            else:
                path = library_file(web, parsed.path.lstrip('/'))
                if path.is_file():
                    mime = 'text/javascript' if path.suffix in ('.js', '.mjs') else 'application/octet-stream'
                    if path.suffix == '.png':
                        mime = 'image/png'
                    await request.fulfill(content_type=mime, path=str(path))
                else:
                    await request.abort()

        await page.route('**/*', route)
        await page.goto('http://127.0.0.1/')
        await page.wait_for_function('typeof window.convertClip === "function"', timeout=60000)
        for index, item in enumerate(items):
            source = library_file(args.source_dir.resolve(), item['file'])
            source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
            if source_hash != item['sha256']:
                raise ValueError(f'FBX 校验失败：{item["file"]}')
            converted_path = cache / (source.stem + '.json')
            if converted_path.exists():
                result = json.loads(converted_path.read_text())
                if result['source_sha256'] != source_hash:
                    raise ValueError(f'转换缓存的源文件已变化：{source.name}')
            else:
                result = await asyncio.wait_for(page.evaluate('(index) => window.convertClip(index)', index), timeout=120)
                result['source_sha256'] = source_hash
                converted_path.write_text(json.dumps(result))
            frames = result['frames']
            features = pose_features(frames)
            selected = select_frames(features, accepted, args.per_clip, args.min_distance)
            accepted = np.concatenate((accepted, features[selected]))
            for frame_index in selected:
                frame = frames[frame_index]
                pose = frame['pose']
                name = f"{item['label_zh']}__{source.stem}__{frame_index:03d}"
                path = library_file(library, Path('local_user_poses', 'poses', 'Mixamo_' + item['category'], name + '.json'))
                description = describe_pose(item, frame['joints'])
                pose['_library'] = {'repository': 'local_user_poses', 'category': 'Mixamo_' + item['category'],
                    'asset_type': 'pose',
                    'tags': [item['category'], item['label_zh'], item['prompt'], 'Mixamo'],
                    'description': description,
                    'source': {'dataset': manifest['dataset'], 'revision': manifest['revision'],
                        'file': item['file'], 'sha256': source_hash, 'time_seconds': frame['time'],
                        'motion_id': item['motion_id'], 'url': item['url']}}
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(pose, ensure_ascii=False, indent=2))
                preview = await page.evaluate('(pose) => window.previewPose(pose)', pose)
                path.with_suffix('.png').write_bytes(base64.b64decode(preview.split(',', 1)[1]))
                report['items'].append({'pose': path.relative_to(library).as_posix(), 'category': item['category'],
                    'label': item['label_zh'], 'description': description, 'source': pose['_library']['source']})
            (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
            print(f"[{index + 1}/{len(items)}] {item['category']} {source.stem}: +{len(selected)}, total {len(report['items'])}", flush=True)
        await browser.close()
    report['counts'] = dict(Counter(item['category'] for item in report['items']))
    (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report['counts'], ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--source-dir', type=Path, required=True)
    parser.add_argument('--dependencies', type=Path, required=True, help='带 SHA256 清单的本地 FBXLoader 模块缓存')
    parser.add_argument('--browser', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True, help='独立输出目录；先检查预览，再安装到姿势库')
    parser.add_argument('--per-clip', type=int, default=4)
    parser.add_argument('--min-distance', type=float, default=0.055)
    parser.add_argument('--limit-clips', type=int)
    args = parser.parse_args()
    if args.per_clip < 1 or args.min_distance <= 0 or (args.limit_clips is not None and args.limit_clips < 1):
        parser.error('数量与去重距离必须为正数')
    asyncio.run(convert(args))


if __name__ == '__main__':
    main()
