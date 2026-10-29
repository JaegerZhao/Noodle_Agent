// Structured personal maps and explicit relationships; IDs come from current library entries.
let modalSequence=0;
function modal(el, title) { const dialog = el('dialog');const heading=el('h2', title);heading.id='modal-title-'+(++modalSequence);dialog.setAttribute('aria-labelledby',heading.id); dialog.append(heading); document.body.append(dialog); dialog.addEventListener('close', () => dialog.remove()); dialog.showModal(); return dialog; }
export async function mapEditor({ api, el, action, note, done, notice, knowledgeRoot=false }) {
    let all = await api('/library?limit=100');
    const personal = note?.kind === 'MOC' && note.map_policy === 'MANUAL';
    const d = modal(el, personal ? '编辑个人地图' : '新建个人地图');
    const label = el('label', '地图名称'), name = el('input');
    name.value = personal ? note.title : note ? '我的 ' + note.title : '';
    label.append(name);
    d.append(label);
    let entries = (note?.map_entries || []).map(e => ({ entry_id: e.entry_id, parent_entry_id: e.parent_entry_id, title: e.title, target_note_id: e.target_note_id, target_revision_id: e.target_revision_id, description: e.description }));
    const rows = el('div');
    d.append(rows);
    function redraw() { rows.replaceChildren(); for (let i = 0; i < entries.length; i++) {
        const entry = entries[i], row = el('div', undefined, 'source-card');
        const title = el('input');
        title.value = entry.title;
        title.setAttribute('aria-label', '条目名称');
        title.oninput = () => entry.title = title.value;
        const parent = el('select');
        parent.setAttribute('aria-label', '上级目录');
        const first = el('option', '顶层');
        first.value = '';
        parent.append(first);
        const descendant=id=>{let cursor=entries.find(e=>e.entry_id===id),seen=new Set();while(cursor){if(cursor.entry_id===entry.entry_id)return true;if(seen.has(cursor.entry_id))return true;seen.add(cursor.entry_id);cursor=entries.find(e=>e.entry_id===cursor.parent_entry_id);}return false;};
        for (const other of entries.filter(x => !descendant(x.entry_id))) {
            const opt = el('option', other.title);
            opt.value = other.entry_id;
            parent.append(opt);
        }
        parent.value = entry.parent_entry_id || '';
        parent.onchange = () => entry.parent_entry_id = parent.value || null;
        const controls = el('div', undefined, 'actions');
        controls.append(action('上移', () => { const j=entries.findLastIndex((e,k)=>k<i&&e.parent_entry_id===entry.parent_entry_id);if (j>=0) {
            [entries[j], entries[i]] = [entries[i], entries[j]];
            redraw();
        } }), action('下移', () => { const j=entries.findIndex((e,k)=>k>i&&e.parent_entry_id===entry.parent_entry_id);if (j>=0) {
            [entries[j], entries[i]] = [entries[i], entries[j]];
            redraw();
        } }), action('移除', () => { const id = entry.entry_id; entries = entries.filter(x => x.entry_id !== id).map(e => e.parent_entry_id === id ? { ...e, parent_entry_id: entry.parent_entry_id } : e); redraw(); }));
        row.append(title, parent, controls);
        rows.append(row);
    } }
    const choices = el('select');
    choices.setAttribute('aria-label', '选择加入地图的笔记');
    const query=el('input');query.type='search';query.placeholder='搜索知识点或笔记';query.setAttribute('aria-label','搜索加入地图的笔记');
    let request=0,nextOffset=100;
    async function search(more=false){const ticket=++request;const result=await api('/library?'+new URLSearchParams({q:query.value,limit:100,offset:more?nextOffset:0}));if(ticket!==request||!d.isConnected)return;all={...result,items:more?[...all.items,...result.items]:result.items};nextOffset=all.items.length;choices.replaceChildren();for(const n of all.items.filter(n=>n.kind!=='MOC')){const option=el('option',n.title);option.value=n.id;choices.append(option);}moreButton.hidden=nextOffset>=result.total;}
    const moreButton=action('加载更多笔记',()=>search(true));moreButton.hidden=all.total<=100;d.append(query,action('搜索',()=>search()),moreButton);
    for (const n of all.items.filter(n => n.kind !== 'MOC')) {
        const option = el('option', n.title);
        option.value = n.id;
        choices.append(option);
    }
    d.append(el('p', '选择知识点或原子笔记加入地图，可搜索或加载更多。', 'muted'), choices, action('加入笔记', () => { const n = all.items.find(x => x.id === choices.value); if (n) {
        entries.push({ entry_id: 'item-' + crypto.randomUUID(), parent_entry_id: null, title: n.title, target_note_id: n.id, description: '' });
        redraw();
    } }), action('添加分组', () => { entries.push({ entry_id: 'group-' + crypto.randomUUID(), parent_entry_id: null, title: '新分组', description: '' }); redraw(); }));
    const controls = el('div', undefined, 'actions');
    controls.append(action('取消', () => d.close()), action('保存地图', async () => { try {
        const result = await api('/maps', { method: 'POST', body: JSON.stringify({ ...(personal ? { note_id: note.id, expected_revision: note.revision.id } : {}), title: name.value, entries, knowledge_root:knowledgeRoot }) });
        d.close();
        notice('地图已保存。');
        await done(result.note_id);
    }
    catch (e) {
        notice(e.message, true);
    } }));
    d.append(controls);
    redraw();
}
export async function relationEditor({ api, el, action, note, done, notice }) {
    const [all, owned] = await Promise.all([api('/library?limit=500'), api('/notes/' + note.id + '/links')]);
    const d = modal(el, '管理双向链接');
    d.append(el('p', '确认链接后，目标笔记会自动显示反向链接。先修关系会检查环路。', 'muted'));
    let links = owned.links.filter(e => e.editable).map(e => ({ ...e.value }));
    const managed = owned.links.filter(e => !e.editable);
    if (managed.length) {
        const system = el('details'), summary = el('summary', `系统关系（${managed.length}）`);
        system.append(summary, el('p', '系统维护的层级和来源关系会保留，不受下方个人链接编辑影响。', 'muted'));
        for (const edge of managed) {
            const value = edge.value;
            const name = edge.target_title || all.items.find(n => n.id === value.target_note_id)?.title || value.target_note_id;
            system.append(el('p', `${name} · ${value.navigation_role === 'TOPIC_PARENT' ? '上级主题' : value.navigation_role === 'TOPIC_CHILD' ? '下级主题' : '知识关联'}`));
        }
        d.append(system);
    }
    const list = el('div');
    function redraw() { list.replaceChildren(); for (let i = 0; i < links.length; i++) {
        const l = links[i], row = el('p');
        row.append(el('span', (all.items.find(n => n.id === l.target_note_id)?.title || l.target_note_id) + ' · ' + l.relation + ' '), action('移除', () => { links.splice(i, 1); redraw(); }));
        list.append(row);
    } }
    const target = el('select');
    target.setAttribute('aria-label', '链接目标');
    for (const n of all.items.filter(n => n.id !== note.id)) {
        const option = el('option', n.title);
        option.value = n.id;
        target.append(option);
    }
    const relation = el('select');
    relation.setAttribute('aria-label', '关系类型');
    for (const [v, t] of [['RELATED_TO', '相关内容'], ['COMPARES_WITH', '对比'], ['PREREQUISITE_OF', '当前内容是目标的先修'], ['CONTRADICTS', '存在矛盾'], ['LINKS_TO', '阅读链接']]) {
        const option = el('option', t);
        option.value = v;
        relation.append(option);
    }
    const basis = el('input');
    basis.placeholder = '为什么建立这个链接？';
    basis.setAttribute('aria-label', '链接依据');
    d.append(list, target, relation, basis, action('添加链接', () => { if (target.value && basis.value.trim()) {
        links.push({ target_note_id: target.value, relation: relation.value, status: 'CONFIRMED', basis: basis.value.trim() });
        redraw();
    } }));
    const controls = el('div', undefined, 'actions');
    controls.append(action('取消', () => d.close()), action('保存链接', async () => { try {
        await api('/notes/' + note.id + '/links', { method: 'PUT', body: JSON.stringify({ expected_revision: owned.expected_revision, editor_contract: owned.editor_contract, links }) });
        d.close();
        notice('链接已保存。');
        await done(note.id);
    }
    catch (e) {
        notice(e.message, true);
    } }));
    d.append(controls);
    redraw();
}
