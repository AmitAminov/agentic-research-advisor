(function(){
  // ---- dashboard: search + area + status + verdict filters ----
  var q=document.getElementById('q');
  var cards=[].slice.call(document.querySelectorAll('.card'));
  if(q||cards.length){
    var area='ALL',status='ALL',verdict='ALL';
    var chips=[].slice.call(document.querySelectorAll('.chip'));
    var visible=cards.slice();
    function apply(){
      var t=(q&&q.value||'').trim().toLowerCase();
      var shown=0;visible=[];
      cards.forEach(function(c){
        var okA=area==='ALL'||c.dataset.code===area;
        var okS=status==='ALL'||c.dataset.status===status||
                (status==='animated'&&c.dataset.anim==='yes');
        var okV=verdict==='ALL'||c.dataset.verdict===verdict;
        var okT=!t||(c.dataset.search||'').indexOf(t)>-1;
        var vis=okA&&okS&&okV&&okT;c.style.display=vis?'':'none';
        if(vis){shown++;visible.push(c);}
      });
      var nr=document.getElementById('noresults');
      if(nr)nr.style.display=shown?'none':'block';
    }
    chips.forEach(function(ch){ch.addEventListener('click',function(){
      if(ch.getAttribute('aria-disabled')==='true')return;
      var group=ch.dataset.group||'area';
      chips.filter(function(x){return (x.dataset.group||'area')===group;})
           .forEach(function(x){x.classList.remove('active');x.setAttribute('aria-pressed','false');});
      ch.classList.add('active');ch.setAttribute('aria-pressed','true');
      if(group==='status')status=ch.dataset.status;
      else if(group==='verdict')verdict=ch.dataset.verdict;
      else area=ch.dataset.area;
      apply();
    });});
    if(q)q.addEventListener('input',apply);

    // keyboard: '/' focuses search; arrows move a roving focus across cards
    function focusCard(i){
      if(!visible.length)return;
      i=Math.max(0,Math.min(visible.length-1,i));
      visible[i].focus();
    }
    document.addEventListener('keydown',function(e){
      if(e.key==='/'&&q&&document.activeElement!==q){e.preventDefault();q.focus();return;}
      var ae=document.activeElement;
      var inSearch=ae===q;
      var onCard=ae&&ae.classList&&ae.classList.contains('card');
      if(!visible.length)return;
      if(inSearch){
        // only ArrowDown leaves the field; Left/Right/Up belong to the caret
        if(e.key==='ArrowDown'){e.preventDefault();focusCard(0);}
        return;
      }
      if((e.key==='ArrowDown'||e.key==='ArrowRight')&&onCard){
        e.preventDefault();
        focusCard(visible.indexOf(ae)+1);
      }else if((e.key==='ArrowUp'||e.key==='ArrowLeft')&&onCard){
        e.preventDefault();
        var idx=visible.indexOf(ae);
        if(idx<=0){if(q){q.focus();}}else{focusCard(idx-1);}
      }
    });
    // make cards keyboard-reachable
    cards.forEach(function(c){if(!c.hasAttribute('tabindex'))c.setAttribute('tabindex','0');});
    apply();
  }

  // ---- paper page: tabs ----
  var tabs=[].slice.call(document.querySelectorAll('.tab'));
  var panels=[].slice.call(document.querySelectorAll('.panel'));
  function activate(key){
    if(!tabs.some(function(t){return t.dataset.tab===key;}))return;
    // pause any clip when leaving the animation tab (stop decoding/looping)
    if(key!=='anim'){
      [].slice.call(document.querySelectorAll('.panel[data-panel="anim"] video'))
        .forEach(function(v){try{v.pause();}catch(_){}});
    }
    tabs.forEach(function(t){var on=t.dataset.tab===key;
      t.classList.toggle('active',on);t.setAttribute('aria-selected',on?'true':'false');});
    panels.forEach(function(p){p.classList.toggle('active',p.dataset.panel===key);});
    if(history.replaceState)history.replaceState(null,'','#'+key);
  }
  tabs.forEach(function(t){t.addEventListener('click',function(){activate(t.dataset.tab);});});
  // any element with data-goto (e.g. verdict pill) jumps to a tab
  [].slice.call(document.querySelectorAll('[data-goto]')).forEach(function(el){
    el.addEventListener('click',function(e){e.preventDefault();activate(el.dataset.goto);
      var top=document.querySelector('.tabs');if(top)top.scrollIntoView({behavior:'smooth',block:'start'});});
  });
  if(tabs.length){
    var h=(location.hash||'').replace('#','');
    if(h)activate(h);
    // [ and ] cycle tabs globally; Arrow/Home/End move selection when a tab is focused
    document.addEventListener('keydown',function(e){
      var lbOpen=document.querySelector('.lightbox.open');
      if(lbOpen)return;
      var act=tabs.filter(function(t){return t.classList.contains('active');})[0];
      var i=tabs.indexOf(act);
      if(e.key==='['||e.key===']'){
        var animOn=document.querySelector('.panel[data-panel="anim"].active');
        if(animOn)return; // anim tab owns [ ] would clash with clips; keep old guard
        if(i<0)return;
        activate(tabs[(i+(e.key===']'?1:tabs.length-1))%tabs.length].dataset.tab);
        return;
      }
      var ae=document.activeElement;
      var onTab=ae&&ae.classList&&ae.classList.contains('tab');
      if(!onTab)return; // arrows only steer tabs while a tab has focus
      var j=tabs.indexOf(ae),n=tabs.length,tgt=-1;
      if(e.key==='ArrowRight'||e.key==='ArrowDown')tgt=(j+1)%n;
      else if(e.key==='ArrowLeft'||e.key==='ArrowUp')tgt=(j-1+n)%n;
      else if(e.key==='Home')tgt=0;
      else if(e.key==='End')tgt=n-1;
      if(tgt>=0){e.preventDefault();activate(tabs[tgt].dataset.tab);tabs[tgt].focus();}
    });
  }

  // ---- cinematic player: filmstrip switching + keyboard ----
  var stage=document.getElementById('cinema-stage');
  var cells=[].slice.call(document.querySelectorAll('.film-cell'));
  var reduceMotion=!!(window.matchMedia&&window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  function showClip(cell){
    cells.forEach(function(c){c.classList.remove('active');});
    cell.classList.add('active');
    var src=cell.dataset.src,name=cell.dataset.name,area=cell.dataset.area,
        lead=cell.dataset.lead||'Reproduced finding';
    var autoplay=reduceMotion?'':' autoplay';
    var media=cell.dataset.gif==='1'
      ? '<img class="anim-media" src="'+src+'" alt="'+name+'" loading="lazy">'
      : '<video class="anim-media" controls loop muted playsinline preload="metadata"'+autoplay+'>'+
        '<source src="'+src+'">Your browser cannot play this video.</video>';
    stage.classList.remove('swap');void stage.offsetWidth;stage.classList.add('swap');
    stage.innerHTML='<figure class="cinema"><div class="cinema-frame">'+media+
      '</div><figcaption class="cinema-cap">'+lead+' — '+area+
      '<span class="mono muted"> · '+name+'</span></figcaption></figure>';
  }
  if(stage&&cells.length){
    cells.forEach(function(cell){cell.addEventListener('click',function(){showClip(cell);});});
    document.addEventListener('keydown',function(e){
      var animOn=document.querySelector('.panel[data-panel="anim"].active');
      if(!animOn)return;
      var vid=stage.querySelector('video');
      if(e.key===' '&&vid){
        var ae=document.activeElement;
        // only toggle when focus isn't on an interactive control (tab/link/cell)
        if(!ae||!/^(A|BUTTON|INPUT|TEXTAREA|SELECT)$/.test(ae.tagName)){
          e.preventDefault();vid.paused?vid.play():vid.pause();
        }
        return;
      }
      if(e.key!=='ArrowLeft'&&e.key!=='ArrowRight')return;
      var af=document.activeElement;
      if(af&&af.classList&&af.classList.contains('tab'))return; // tabs own arrows when focused
      e.preventDefault();
      var i=cells.map(function(c){return c.classList.contains('active');}).indexOf(true);
      if(i<0)i=0;
      showClip(cells[(i+(e.key==='ArrowRight'?1:cells.length-1))%cells.length]);
    });
  }

  // ---- figure lightbox: gallery nav + captions ----
  var lb=document.getElementById('lightbox'),lbimg=document.getElementById('lbimg'),
      lbcap=document.getElementById('lbcap');
  if(lb&&lbimg){
    var zoomers=[],cur=0,lastFocus=null;
    var pv=document.getElementById('lbprev'),nx=document.getElementById('lbnext'),
        cl=document.getElementById('lbclose');
    function refresh(){
      zoomers=[].slice.call(document.querySelectorAll('.zoom')).filter(function(z){
        return z.offsetParent!==null; // only visible (active panel)
      });
    }
    function paint(){
      var z=zoomers[cur];if(!z)return;
      lbimg.src=z.dataset.full||z.src;
      var cap=z.dataset.cap||'';
      lbimg.alt=cap||z.alt||'zoomed figure';
      if(lbcap){lbcap.textContent=cap;lbcap.style.display=cap?'':'none';}
    }
    function open(z){refresh();cur=Math.max(0,zoomers.indexOf(z));paint();
      lastFocus=document.activeElement;
      lb.classList.add('open');lb.setAttribute('aria-hidden','false');
      document.body.style.overflow='hidden';
      if(cl)cl.focus();}
    function close(){lb.classList.remove('open');lb.setAttribute('aria-hidden','true');
      document.body.style.overflow='';
      if(lastFocus&&lastFocus.focus)lastFocus.focus();lastFocus=null;}
    function step(d){if(!zoomers.length)return;cur=(cur+d+zoomers.length)%zoomers.length;paint();}
    // click or keyboard (Enter/Space) on a figure opens the viewer
    document.addEventListener('click',function(e){
      var el=e.target;
      if(el.classList&&el.classList.contains('zoom')){e.preventDefault();open(el);}
    });
    document.addEventListener('keydown',function(e){
      var el=e.target;
      if(el&&el.classList&&el.classList.contains('zoom')&&(e.key==='Enter'||e.key===' ')){
        e.preventDefault();open(el);
      }
    });
    if(pv)pv.addEventListener('click',function(e){e.stopPropagation();step(-1);});
    if(nx)nx.addEventListener('click',function(e){e.stopPropagation();step(1);});
    if(cl)cl.addEventListener('click',function(e){e.stopPropagation();close();});
    lb.addEventListener('click',function(e){
      if(e.target===lb||e.target===lbimg||(e.target.classList&&e.target.classList.contains('lb-fig')))close();
    });
    document.addEventListener('keydown',function(e){
      if(!lb.classList.contains('open'))return;
      if(e.key==='Escape'){close();return;}
      if(e.key==='ArrowRight'){e.preventDefault();step(1);return;}
      if(e.key==='ArrowLeft'){e.preventDefault();step(-1);return;}
      if(e.key==='Tab'){
        // trap Tab within the lightbox controls (close / prev / next)
        var f=[cl,pv,nx].filter(function(b){return b;});
        if(!f.length)return;
        e.preventDefault();
        var idx=f.indexOf(document.activeElement);
        idx=e.shiftKey?(idx<=0?f.length-1:idx-1):(idx>=f.length-1?0:idx+1);
        f[idx].focus();
      }
    });
  }
})();
