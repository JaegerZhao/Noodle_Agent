// Keep identical message nodes mounted, preserving details, focus and selection.
export function reconcileChildren(parent,nodes){
 const keep=new Set(nodes);
 for(const node of [...parent.children])if(!keep.has(node))node.remove();
 nodes.forEach((node,index)=>{if(parent.children[index]!==node)parent.insertBefore(node,parent.children[index]||null);});
}
