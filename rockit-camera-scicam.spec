Name:      rockit-camera-scicam
Version:   %{_version}
Release:   1%{dist}
Summary:   Control code for PIRT SciCam1280
Url:       https://github.com/rockit-astro/camd-scicam
License:   GPL-3.0
BuildArch: noarch

%description


%build
mkdir -p %{buildroot}%{_bindir}
mkdir -p %{buildroot}%{_unitdir}
mkdir -p %{buildroot}%{_sysconfdir}/camd
mkdir -p %{buildroot}%{_udevrulesdir}

%{__install} %{_sourcedir}/scicam_camd %{buildroot}%{_bindir}
%{__install} %{_sourcedir}/scicam_camd@.service %{buildroot}%{_unitdir}

%{__install} %{_sourcedir}/swir.json %{buildroot}%{_sysconfdir}/camd
%{__install} %{_sourcedir}/scicam.fmt %{buildroot}%{_sysconfdir}/camd

%package server
Summary:  SciCam camera server
Group:    Unspecified
Requires: python3-rockit-camera-scicam
%description server

%files server
%defattr(0755,root,root,-)
%{_bindir}/scicam_camd
%defattr(0644,root,root,-)
%{_unitdir}/scicam_camd@.service

%package data-clasp
Summary: SciCam camera data for the CLASP telescope
Group:   Unspecified
Requires: python3-rockit-camera-scicam

%description data-clasp

%files data-clasp
%defattr(0644,root,root,-)
%{_sysconfdir}/camd/swir.json
%{_sysconfdir}/camd/scicam.fmt

%changelog
