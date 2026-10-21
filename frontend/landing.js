/**
 * Project AURA — Landing Page Interactive Engine
 */

document.addEventListener('DOMContentLoaded', () => {
  // 0. Theme Switcher (Light / Dark)
  const themeToggleBtn = document.getElementById('theme-toggle-btn');
  const themeLabel = document.getElementById('theme-toggle-label');
  const mobileDrawerThemeBtn = document.getElementById('mobile-drawer-theme-btn');
  const mobileDrawerThemeLabel = document.getElementById('mobile-drawer-theme-label');

  function updateThemeUI(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    localStorage.setItem('aura-color-theme', theme);
    if (themeLabel) {
      themeLabel.textContent = theme === 'light' ? 'Dark' : 'Light';
    }
    if (mobileDrawerThemeLabel) {
      mobileDrawerThemeLabel.textContent = theme === 'light' ? 'Switch to Dark Mode' : 'Switch to Light Mode';
    }
  }

  const currentTheme = localStorage.getItem('aura-color-theme') || 'dark';
  updateThemeUI(currentTheme);

  function toggleTheme() {
    const activeTheme = document.documentElement.getAttribute('data-theme') || 'dark';
    const newTheme = activeTheme === 'light' ? 'dark' : 'light';
    updateThemeUI(newTheme);
  }

  if (themeToggleBtn) {
    themeToggleBtn.addEventListener('click', toggleTheme);
  }
  if (mobileDrawerThemeBtn) {
    mobileDrawerThemeBtn.addEventListener('click', toggleTheme);
  }

  // 1. Yellow Bento Card Tab Switcher with 5s Auto-Rotation
  const tabItems = document.querySelectorAll('.yellow-tab-item');
  const focusTitle = document.getElementById('yellow-focus-title');
  const focusDesc = document.getElementById('yellow-focus-desc');
  const yellowCounterBadge = document.getElementById('yellow-tab-counter');
  const yellowCard = document.getElementById('card-yellow-container');

  const yellowTabContent = [
    {
      title: "Aura turns moments into focus",
      desc: "Bee remembers what you forgot. AURA anticipates what you're about to forget, and stops the world from distracting your deep work flow."
    },
    {
      title: "Aura prepares you before the moment",
      desc: "Most meeting tools help after the meeting is over. AURA pre-briefs you at T-5 with synthesized context, tone calibration, and 3 high-impact questions to ask."
    },
    {
      title: "Aura is ready when you need it",
      desc: "Need to silence distractions? Have a question about your focus trends? Want to leave a quick thought for later? Talk to AURA through the wearable companion, web control plane, or Apple Watch."
    }
  ];

  let currentYellowTabIndex = 0;
  let yellowTabTimer = null;
  let isYellowHovered = false;

  function selectYellowTab(index, restartTimer = false) {
    if (!yellowTabContent[index]) return;
    currentYellowTabIndex = index;

    tabItems.forEach((t, i) => {
      const isSelected = i === index;
      t.classList.toggle('active', isSelected);
      t.setAttribute('aria-selected', isSelected ? 'true' : 'false');
      
      // Reset animation on active progress line
      if (isSelected) {
        const line = t.querySelector('.tab-progress-line');
        if (line) {
          line.style.animation = 'none';
          void t.offsetHeight; // trigger reflow
          line.style.animation = '';
        }
      }
    });

    if (yellowCounterBadge) {
      yellowCounterBadge.textContent = `${index + 1} of ${yellowTabContent.length} · Auto Rotating`;
    }

    if (focusTitle && focusDesc) {
      focusTitle.style.transition = 'opacity 0.15s ease, transform 0.15s ease';
      focusDesc.style.transition = 'opacity 0.15s ease, transform 0.15s ease';
      focusTitle.style.opacity = '0';
      focusTitle.style.transform = 'translateY(4px)';
      focusDesc.style.opacity = '0';
      focusDesc.style.transform = 'translateY(4px)';

      setTimeout(() => {
        focusTitle.textContent = yellowTabContent[index].title;
        focusDesc.textContent = yellowTabContent[index].desc;
        focusTitle.style.opacity = '1';
        focusTitle.style.transform = 'translateY(0)';
        focusDesc.style.opacity = '1';
        focusDesc.style.transform = 'translateY(0)';
      }, 150);
    }

    if (restartTimer) {
      startYellowTabTimer();
    }
  }

  function startYellowTabTimer() {
    clearInterval(yellowTabTimer);
    yellowTabTimer = setInterval(() => {
      if (!isYellowHovered) {
        const nextIndex = (currentYellowTabIndex + 1) % yellowTabContent.length;
        selectYellowTab(nextIndex, false);
      }
    }, 5000);
  }

  tabItems.forEach((tab, index) => {
    tab.addEventListener('click', () => selectYellowTab(index, true));
    tab.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        selectYellowTab(index, true);
      }
    });
  });

  if (yellowCard) {
    yellowCard.addEventListener('mouseenter', () => {
      isYellowHovered = true;
    });
    yellowCard.addEventListener('mouseleave', () => {
      isYellowHovered = false;
    });
  }

  // Initialize yellow tab auto rotation
  startYellowTabTimer();

  // 2. Interactive To-Dos / Focus Actions
  const todoItems = document.querySelectorAll('.todo-main-row');
  todoItems.forEach(row => {
    row.addEventListener('click', () => {
      const checkbox = row.querySelector('.todo-checkbox');
      const label = row.querySelector('.todo-label');
      
      if (checkbox && label) {
        checkbox.classList.toggle('checked');
        label.classList.toggle('completed');
      }
    });
  });

  // 3. Interactive Chat Suggestions
  const suggestionPills = document.querySelectorAll('.suggestion-pill');
  const chatBubbleUser = document.querySelector('.bubble-dark-user');
  const chatAiResponse = document.getElementById('chat-ai-response-text');

  const responses = {
    "What were yesterday's architecture decisions?": {
      user: "Show decisions from yesterday's AWS architecture sync",
      ai: "3 key decisions: 1) Bedrock Claude 3.5 Sonnet request token cap set to 1,000. 2) DynamoDB single-table TTL set to 30 days. 3) Alexa Fire TV pause adapter verified with reversible undo."
    },
    "Prepare me for the 10:00 AM platform review": {
      user: "Prepare me for the 10:00 AM AWS platform review",
      ai: "Synthesized T-5 brief ready. Tone calibrated to constructive technical review. Prepared 3 questions on client-side minimization, capability adapter fallback, and SQS DLQ retry policies."
    },
    "Check my Deep Work Bank & focus stats": {
      user: "Show my Deep Work Bank focus score for this week",
      ai: "You guarded 18 deep work blocks (450 total minutes) this week. Deep Work Bank balance: +36 points earned. This data is strictly private and never exposed to employers."
    }
  };

  suggestionPills.forEach(pill => {
    pill.addEventListener('click', () => {
      suggestionPills.forEach(p => p.classList.remove('active'));
      pill.classList.add('active');
      const text = pill.textContent.trim();
      if (responses[text]) {
        if (chatBubbleUser) {
          chatBubbleUser.textContent = `"${responses[text].user}"`;
        }
        if (chatAiResponse) {
          chatAiResponse.textContent = responses[text].ai;
        }
      }
    });
  });

  // 4. Aesthetic Card Image Toggle & 5s Auto-Rotation
  const btnShowPackets = document.getElementById('btn-show-packets');
  const btnShowFleet = document.getElementById('btn-show-fleet');
  const aestheticCardImg = document.getElementById('aesthetic-card-img');
  const imageCaptionTag = document.getElementById('image-caption-tag');
  const imageSubCaption = document.getElementById('image-sub-caption');
  const darkCard = document.getElementById('card-dark-aesthetic-container');

  const imageData = [
    {
      src: 'assets/aura-bedroom-sync.jpg',
      alt: 'Aura smart watch transmitting smooth ambient focus packets to bedroom TV and Echo speaker',
      tag: 'BEDROOM AMBIENT MESH',
      sub: 'Watch · Echo Dot · Fire TV Sync'
    },
    {
      src: 'assets/aura-device-fleet.jpg',
      alt: 'Project AURA certified 5-device hardware fleet lineup',
      tag: '5-DEVICE ECOSYSTEM',
      sub: 'Wearable Band · Watch · Fire TV · Echo · Laptop'
    }
  ];

  let currentImageIndex = 0;
  let imageTimer = null;
  let isImageHovered = false;

  function switchAestheticImage(index, restartTimer = false) {
    if (!imageData[index]) return;
    currentImageIndex = index;
    const isPackets = index === 0;

    if (btnShowPackets && btnShowFleet) {
      btnShowPackets.classList.toggle('active', isPackets);
      btnShowPackets.setAttribute('aria-pressed', isPackets ? 'true' : 'false');
      btnShowFleet.classList.toggle('active', !isPackets);
      btnShowFleet.setAttribute('aria-pressed', !isPackets ? 'true' : 'false');

      // Trigger restart on the progress bar inside the active pill
      const activeBtn = isPackets ? btnShowPackets : btnShowFleet;
      const bar = activeBtn.querySelector('.pill-progress-bar');
      if (bar) {
        bar.style.animation = 'none';
        void activeBtn.offsetHeight; // trigger reflow
        bar.style.animation = '';
      }
    }

    if (aestheticCardImg) {
      aestheticCardImg.style.opacity = '0';
      aestheticCardImg.style.transform = 'scale(0.99)';
      setTimeout(() => {
        aestheticCardImg.src = imageData[index].src;
        aestheticCardImg.alt = imageData[index].alt;
        if (imageCaptionTag) imageCaptionTag.textContent = imageData[index].tag;
        if (imageSubCaption) imageSubCaption.textContent = imageData[index].sub;
        aestheticCardImg.style.opacity = '1';
        aestheticCardImg.style.transform = 'scale(1)';
      }, 150);
    }

    if (restartTimer) {
      startImageTimer();
    }
  }

  function startImageTimer() {
    clearInterval(imageTimer);
    imageTimer = setInterval(() => {
      if (!isImageHovered) {
        const nextIndex = (currentImageIndex + 1) % imageData.length;
        switchAestheticImage(nextIndex, false);
      }
    }, 5000);
  }

  if (btnShowPackets) {
    btnShowPackets.addEventListener('click', () => switchAestheticImage(0, true));
  }

  if (btnShowFleet) {
    btnShowFleet.addEventListener('click', () => switchAestheticImage(1, true));
  }

  if (darkCard) {
    darkCard.addEventListener('mouseenter', () => {
      isImageHovered = true;
    });
    darkCard.addEventListener('mouseleave', () => {
      isImageHovered = false;
    });
  }

  // 4. Hero Corner Video Link handler
  const heroCornerVideo = document.getElementById('hero-corner-video');
  if (heroCornerVideo) {
    heroCornerVideo.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        window.open(heroCornerVideo.href, '_blank', 'noopener,noreferrer');
      }
    });
  }

  // 5. Mobile Navigation Drawer Controls
  const mobileMenuBtn = document.getElementById('mobile-menu-btn');
  const mobileMenuClose = document.getElementById('mobile-menu-close');
  const mobileNavDrawer = document.getElementById('mobile-nav-drawer');
  const mobileNavBackdrop = document.getElementById('mobile-nav-backdrop');

  function openMobileMenu() {
    if (mobileNavDrawer && mobileNavBackdrop) {
      mobileNavDrawer.classList.add('active');
      mobileNavBackdrop.classList.add('active');
      mobileNavDrawer.setAttribute('aria-hidden', 'false');
      if (mobileMenuBtn) {
        mobileMenuBtn.setAttribute('aria-expanded', 'true');
      }
      document.body.style.overflow = 'hidden';
    }
  }

  function closeMobileMenu() {
    if (mobileNavDrawer && mobileNavBackdrop) {
      mobileNavDrawer.classList.remove('active');
      mobileNavBackdrop.classList.remove('active');
      mobileNavDrawer.setAttribute('aria-hidden', 'true');
      if (mobileMenuBtn) {
        mobileMenuBtn.setAttribute('aria-expanded', 'false');
      }
      document.body.style.overflow = '';
    }
  }

  if (mobileMenuBtn) {
    mobileMenuBtn.addEventListener('click', () => {
      const isOpen = mobileNavDrawer && mobileNavDrawer.classList.contains('active');
      if (isOpen) {
        closeMobileMenu();
      } else {
        openMobileMenu();
      }
    });
  }

  if (mobileMenuClose) {
    mobileMenuClose.addEventListener('click', closeMobileMenu);
  }

  if (mobileNavBackdrop) {
    mobileNavBackdrop.addEventListener('click', closeMobileMenu);
  }

  // Close mobile drawer when clicking any link within it
  if (mobileNavDrawer) {
    const drawerLinks = mobileNavDrawer.querySelectorAll('a');
    drawerLinks.forEach(link => {
      link.addEventListener('click', closeMobileMenu);
    });
  }

  // Escape key closes mobile menu
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && mobileNavDrawer && mobileNavDrawer.classList.contains('active')) {
      closeMobileMenu();
    }
  });

  // 6. Sticky Bottom Banner Dismissal
  const bannerDismissBtn = document.getElementById('banner-dismiss-btn');
  const stickyBanner = document.getElementById('sticky-bottom-banner');
  if (bannerDismissBtn && stickyBanner) {
    bannerDismissBtn.addEventListener('click', () => {
      stickyBanner.style.transition = 'transform 0.3s ease, opacity 0.3s ease';
      stickyBanner.style.transform = 'translateY(100%)';
      stickyBanner.style.opacity = '0';
      setTimeout(() => {
        stickyBanner.style.display = 'none';
      }, 300);
    });
  }

  // Initialize image auto rotation
  startImageTimer();
});
